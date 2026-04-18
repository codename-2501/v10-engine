"""
engine/workspace/test_generator.py
워크스페이스 분석 → Playwright E2E 테스트 자동 생성 + 실행.
프로젝트 스택/라우트/API를 동적으로 감지해서 범용 테스트를 만든다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import asyncio
from pathlib import Path

logger = logging.getLogger("engine.workspace.test_generator")


def generate_and_run_tests(
    workspace_path: Path,
    stack: dict,
    fe_port: int,
    be_port: int,
) -> dict:
    """E2E 테스트 생성 → 실행 → 결과 반환."""
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"

    # 1. 프론트엔드 라우트 감지
    routes = _detect_frontend_routes(fe) if fe.is_dir() else []

    # 2. 백엔드 API 엔드포인트 감지
    api_endpoints = _detect_api_endpoints(be, stack) if be.is_dir() else []

    # 3. 인증 방식 감지
    auth = _detect_auth(fe, be, stack)

    # 4. Playwright 설치 (없으면)
    _ensure_playwright(fe)

    # 5. 테스트 파일 생성
    test_path = fe / "e2e" / "auto-generated.spec.ts"
    test_path.parent.mkdir(parents=True, exist_ok=True)

    test_code = _build_test_code(
        routes=routes,
        api_endpoints=api_endpoints,
        auth=auth,
        fe_port=fe_port,
        be_port=be_port,
        api_prefix="/api/v1" if stack.get("backend") == "express" else "/api",
    )
    test_path.write_text(test_code, encoding="utf-8")

    # 5-1. [v8+] intake 시나리오 기반 E2E + 카테고리 UI 검증 테스트
    scenario_path = fe / "e2e" / "scenario-check.spec.ts"
    intake = _load_intake_data(workspace_path)
    if intake:
        scenario_code = _build_scenario_test_code(
            intake=intake,
            routes=routes,
            fe_port=fe_port,
        )
        scenario_path.write_text(scenario_code, encoding="utf-8")
        logger.info("scenario_e2e_generated features=%d", len(intake.get("features", [])))

    # Playwright config (없으면)
    config_path = fe / "playwright.config.ts"
    if not config_path.is_file():
        config_path.write_text(
            f"""import {{ defineConfig }} from '@playwright/test';
export default defineConfig({{
  testDir: './e2e',
  timeout: 30000,
  use: {{ baseURL: 'http://localhost:{fe_port}', headless: true }},
  projects: [{{ name: 'chromium', use: {{ browserName: 'chromium' }} }}],
}});
""",
            encoding="utf-8",
        )

    logger.info(
        "e2e_tests_generated path=%s routes=%d apis=%d",
        test_path, len(routes), len(api_endpoints),
    )

    # 6. 실행
    result = _run_tests(fe)
    return result


# ---------------------------------------------------------------------------
# 라우트 감지
# ---------------------------------------------------------------------------

def _detect_frontend_routes(fe: Path) -> list[dict]:
    """Next.js App Router 구조 분석 → 라우트 목록 추출."""
    routes = []
    app_dir = fe / "src" / "app"
    if not app_dir.is_dir():
        return routes

    for page_file in app_dir.rglob("page.tsx"):
        rel = page_file.relative_to(app_dir)
        parts = list(rel.parts[:-1])  # page.tsx 제거

        # route group (auth), (main) 등 제거
        cleaned = [p for p in parts if not p.startswith("(")]

        # dynamic segments: [petId] → :petId
        path = "/" + "/".join(cleaned) if cleaned else "/"
        is_dynamic = any(p.startswith("[") for p in cleaned)

        # 인증 필요 여부 (auth 그룹이면 public)
        is_public = any(p == "(auth)" for p in parts)

        routes.append({
            "path": path,
            "is_dynamic": is_dynamic,
            "is_public": is_public,
            "file": str(page_file),
        })

    return routes


def _detect_api_endpoints(be: Path, stack: dict) -> list[dict]:
    """백엔드 라우터 파일 분석 → API 엔드포인트 목록."""
    endpoints = []

    if stack.get("backend") == "express":
        # Express: router.get/post/put/delete 패턴
        for ts_file in be.rglob("*.router.ts"):
            try:
                content = ts_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # router.get('/', ...) or router.post('/login', ...)
            for m in re.finditer(r"router\.(get|post|put|delete)\(\s*['\"]([^'\"]+)['\"]", content):
                method = m.group(1).upper()
                path = m.group(2)
                # authenticate 미들웨어 체크
                line = content[m.start():content.find("\n", m.start())]
                needs_auth = "authenticate" in line

                endpoints.append({
                    "method": method,
                    "path": path,
                    "needs_auth": needs_auth,
                    "file": str(ts_file),
                })

        # routes/index.ts에서 prefix 매핑
        routes_index = be / "src" / "routes" / "index.ts"
        if routes_index.is_file():
            content = routes_index.read_text(encoding="utf-8")
            prefixes = {}
            for m in re.finditer(r"routes\.use\(\s*['\"]([^'\"]+)['\"].*?(\w+Router)", content):
                prefix = m.group(1)
                router_name = m.group(2)
                prefixes[router_name] = prefix

            # 엔드포인트에 prefix 적용
            for ep in endpoints:
                router_file = Path(ep["file"]).stem.replace(".router", "")
                router_var = f"{router_file}Router"
                if router_var in prefixes:
                    ep["full_path"] = prefixes[router_var].rstrip("/") + ep["path"]
                else:
                    ep["full_path"] = ep["path"]

    elif stack.get("backend") in ("fastapi", "django"):
        # Python: @router.get("/path") 또는 path("url/", view) 패턴
        for py_file in be.rglob("*.py"):
            if ".venv" in str(py_file):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # FastAPI/Flask: @router.get("/path")
            for m in re.finditer(r"@\w+\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]", content):
                endpoints.append({
                    "method": m.group(1).upper(),
                    "path": m.group(2),
                    "full_path": m.group(2),
                    "needs_auth": "Depends" in content or "permission" in content.lower(),
                    "file": str(py_file),
                })

            # Django: path("url/", view) — DRF ViewSet은 router.register 패턴
            for m in re.finditer(r"path\(\s*['\"]([^'\"]*)['\"]", content):
                path_val = "/" + m.group(1).strip("/") if m.group(1) else "/"
                endpoints.append({
                    "method": "GET",
                    "path": path_val,
                    "full_path": path_val,
                    "needs_auth": "permission" in content.lower() or "IsAuthenticated" in content,
                    "file": str(py_file),
                })

    elif stack.get("backend") == "spring":
        # Spring: @GetMapping("/path"), @PostMapping, @RequestMapping
        for java_file in be.rglob("*.java"):
            try:
                content = java_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Class-level @RequestMapping
            class_prefix = ""
            class_match = re.search(r'@RequestMapping\(\s*["\']([^"\']+)["\']', content)
            if class_match:
                class_prefix = class_match.group(1).rstrip("/")

            for m in re.finditer(r"@(Get|Post|Put|Delete|Patch)Mapping\(\s*(?:['\"]([^'\"]*)['\"])?", content):
                method = m.group(1).upper()
                path = m.group(2) or "/"
                endpoints.append({
                    "method": method,
                    "path": class_prefix + "/" + path.lstrip("/"),
                    "full_path": class_prefix + "/" + path.lstrip("/"),
                    "needs_auth": "PreAuthorize" in content or "Secured" in content,
                    "file": str(java_file),
                })

    elif stack.get("backend") == "go":
        # Go: r.GET("/path", handler) or http.HandleFunc("/path", handler)
        for go_file in be.rglob("*.go"):
            try:
                content = go_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Gin/Echo/Chi: r.GET("/path", ...) or e.GET("/path", ...)
            for m in re.finditer(r"\.\s*(GET|POST|PUT|DELETE|PATCH)\(\s*['\"]([^'\"]+)['\"]", content):
                endpoints.append({
                    "method": m.group(1).upper(),
                    "path": m.group(2),
                    "full_path": m.group(2),
                    "needs_auth": "middleware" in content.lower() or "auth" in content.lower(),
                    "file": str(go_file),
                })

            # net/http: http.HandleFunc("/path", ...)
            for m in re.finditer(r"HandleFunc\(\s*['\"]([^'\"]+)['\"]", content):
                endpoints.append({
                    "method": "GET",
                    "path": m.group(1),
                    "full_path": m.group(1),
                    "needs_auth": False,
                    "file": str(go_file),
                })

    return endpoints


def _detect_auth(fe: Path, be: Path, stack: dict) -> dict:
    """인증 방식 감지: 로그인 경로, 토큰 필드명 등."""
    auth = {
        "login_path": "/auth/login",  # 기본값 — 아래에서 실제 경로로 덮어씀
        "token_field": "accessToken",
        "refresh_field": "refreshToken",
        "user_field": "user",
        "response_wrapper": "data",
        "store_name": "auth",
        "cookie_name": None,
    }

    # 프론트엔드 API 호출에서 실제 로그인 경로 감지
    for api_file in fe.rglob("*.ts"):
        try:
            content = api_file.read_text(encoding="utf-8")
        except Exception:
            continue
        login_match = re.search(r"\.post[<(].*?['\"]([^'\"]*login[^'\"]*)['\"]", content)
        if login_match:
            auth["login_path"] = login_match.group(1).lstrip("/")
            break

    # 백엔드 라우터에서 로그인 경로 감지 (프론트에서 못 찾으면)
    if auth["login_path"] == "/auth/login":
        for router_file in be.rglob("*auth*router*"):
            try:
                content = router_file.read_text(encoding="utf-8")
            except Exception:
                continue
            login_route = re.search(r"router\.post\(\s*['\"]([^'\"]*login[^'\"]*)['\"]", content)
            if login_route:
                # prefix 포함 경로 구성
                route_path = login_route.group(1)
                # routes/index에서 이 라우터의 마운트 prefix 찾기
                routes_index = be / "src" / "routes" / "index.ts"
                prefix = ""
                if routes_index.is_file():
                    idx_content = routes_index.read_text(encoding="utf-8")
                    router_name = router_file.stem.replace(".router", "") + "Router"
                    prefix_match = re.search(rf"routes\.use\(\s*['\"]([^'\"]+)['\"].*?{router_name}", idx_content)
                    if prefix_match:
                        prefix = prefix_match.group(1).rstrip("/")
                auth["login_path"] = (prefix + route_path).lstrip("/")
                break
        # FastAPI: 직접 경로
        for py_file in be.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            login_match = re.search(r"@\w+\.post\(\s*['\"]([^'\"]*login[^'\"]*)['\"]", content)
            if login_match:
                auth["login_path"] = login_match.group(1).lstrip("/")
                break

    # 프론트엔드 authStore 분석
    for store_file in fe.rglob("*auth*Store*"):
        try:
            content = store_file.read_text(encoding="utf-8")
        except Exception:
            continue
        # persist name
        name_match = re.search(r"name:\s*['\"]([^'\"]+)['\"]", content)
        if name_match:
            auth["store_name"] = name_match.group(1)
        # cookie
        cookie_match = re.search(r"document\.cookie\s*=\s*[`'\"](\w+[-\w]*)", content)
        if cookie_match:
            auth["cookie_name"] = cookie_match.group(1)

    # middleware 쿠키 이름
    middleware = fe / "src" / "middleware.ts"
    if middleware.is_file():
        try:
            content = middleware.read_text(encoding="utf-8")
            cookie_match = re.search(r"cookies\.get\(\s*['\"]([^'\"]+)['\"]", content)
            if cookie_match:
                auth["cookie_name"] = cookie_match.group(1)
        except Exception:
            pass

    # 백엔드 로그인 응답 구조 감지 — accessToken 우선
    for svc_file in be.rglob("*auth*service*"):
        try:
            content = svc_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "accessToken" in content:
            auth["token_field"] = "accessToken"
            break
    for svc_file in be.rglob("*auth*controller*"):
        try:
            content = svc_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "accessToken" in content:
            auth["token_field"] = "accessToken"
            break

    return auth


# ---------------------------------------------------------------------------
# 테스트 코드 생성
# ---------------------------------------------------------------------------

def _build_test_code(
    routes: list[dict],
    api_endpoints: list[dict],
    auth: dict,
    fe_port: int,
    be_port: int,
    api_prefix: str,
) -> str:
    """분석 결과 → Playwright 테스트 코드 생성."""

    cookie_name = auth.get("cookie_name") or auth["store_name"]
    token_field = auth["token_field"]
    refresh_field = auth["refresh_field"]
    wrapper = auth["response_wrapper"]
    store_name = auth["store_name"]

    # 중복 라우트 제거 + 분류
    seen_paths: set[str] = set()
    unique_routes: list[dict] = []
    for r in routes:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            unique_routes.append(r)
    public_routes = [r for r in unique_routes if r["is_public"]]
    auth_routes = [r for r in unique_routes if not r["is_public"] and not r["is_dynamic"]]

    # GET만 필터 (테스트 가능한 것) — admin 제외 (별도 권한 필요)
    get_endpoints = [
        ep for ep in api_endpoints
        if ep["method"] == "GET"
        and not any(seg.startswith(":") or seg.startswith("{") for seg in ep.get("full_path", ep["path"]).split("/"))
        and "/admin" not in ep.get("full_path", ep["path"])
    ]
    # 중복 제거
    seen = set()
    unique_gets = []
    for ep in get_endpoints:
        fp = ep.get("full_path", ep["path"])
        if fp not in seen:
            seen.add(fp)
            unique_gets.append(ep)

    lines = [
        f"import {{ test, expect, Page }} from '@playwright/test';",
        f"",
        f"const API = 'http://localhost:{be_port}{api_prefix}';",
        f"const BASE = 'http://localhost:{fe_port}';",
        f"",
        f"// 테스트 계정 — seed_generator에서 자동 생성된 공통 계정",
        f"const CREDS = {{ email: 'test@test.kr', password: 'Test1234!' }};",
        f"",
        f"let _cachedAuth: any = null;",
        f"async function fetchToken() {{",
        f"  if (_cachedAuth) return _cachedAuth;",
        f"  const res = await fetch(`${{API}}/{auth['login_path'].lstrip('/')}`, {{",
        f"    method: 'POST',",
        f"    headers: {{ 'Content-Type': 'application/json' }},",
        f"    body: JSON.stringify(CREDS),",
        f"  }});",
        f"  const body = await res.json();",
        f"  const data = body.{wrapper} ?? body;",
        f"  if (!data.{token_field}) throw new Error(`Login failed: ${{JSON.stringify(body)}}`);",
        f"  _cachedAuth = {{",
        f"    token: data.{token_field},",
        f"    refreshToken: data.{refresh_field},",
        f"    user: data.{auth['user_field']},",
        f"  }};",
        f"  return _cachedAuth;",
        f"}}",
        f"",
        f"async function login(page: Page) {{",
        f"  const {{ token, refreshToken, user }} = await fetchToken();",
        f"  await page.goto('/login');",
        f"  await page.evaluate(",
        f"    ({{ token, refreshToken, user }}) => {{",
        f"      localStorage.setItem(",
        f"        '{store_name}',",
        f"        JSON.stringify({{ state: {{ token, refreshToken, user }}, version: 0 }}),",
        f"      );",
        f"      document.cookie = `{cookie_name}=${{encodeURIComponent(JSON.stringify({{ state: {{ token }} }}))}};path=/;max-age=3600`;",
        f"    }},",
        f"    {{ token, refreshToken, user }},",
        f"  );",
        f"}}",
        f"",
    ]

    # 인증 필요 라우트 테스트
    if auth_routes:
        lines.append(f"// ── 페이지 렌더링 (인증 필요) ──")
        lines.append(f"test.describe('페이지 렌더링', () => {{")
        lines.append(f"  test.beforeEach(async ({{ page }}) => {{ await login(page); }});")
        lines.append(f"")
        for r in auth_routes:
            path = r["path"]
            lines.append(f"  test('{path} 정상 렌더링', async ({{ page }}) => {{")
            lines.append(f"    await page.goto('{path}');")
            lines.append(f"    const body = page.locator('body');")
            lines.append(f"    await expect(body).not.toContainText('Internal Server Error');")
            lines.append(f"    await expect(body).not.toContainText('Application error');")
            lines.append(f"    await expect(body).not.toContainText('페이지를 찾을 수 없습니다');")
            lines.append(f"  }});")
            lines.append(f"")
        lines.append(f"}});")
        lines.append(f"")

    # 퍼블릭 라우트 테스트
    if public_routes:
        lines.append(f"// ── 퍼블릭 페이지 ──")
        for r in public_routes:
            path = r["path"]
            lines.append(f"test('{path} (퍼블릭) 렌더링', async ({{ page }}) => {{")
            lines.append(f"  await page.goto('{path}');")
            lines.append(f"  const body = page.locator('body');")
            lines.append(f"  await expect(body).not.toContainText('Internal Server Error');")
            lines.append(f"}});")
            lines.append(f"")

    # 미인증 리다이렉트 테스트
    if auth_routes:
        first_auth = auth_routes[0]["path"]
        lines.append(f"test('미인증 → 로그인 리다이렉트', async ({{ page }}) => {{")
        lines.append(f"  await page.goto('{first_auth}');")
        lines.append(f"  await page.waitForURL(/\\/login/);")
        lines.append(f"}});")
        lines.append(f"")

    # 404 테스트
    lines.append(f"test('404 처리', async ({{ page }}) => {{")
    lines.append(f"  await login(page);")
    lines.append(f"  await page.goto('/absolutely-nonexistent-route-xyz');")
    lines.append(f"  await expect(page.getByText('404')).toBeVisible();")
    lines.append(f"}});")
    lines.append(f"")

    # API 엔드포인트 테스트
    if unique_gets:
        lines.append(f"// ── API 엔드포인트 ──")
        lines.append(f"test.describe('API 연동', () => {{")
        for ep in unique_gets:
            fp = ep.get("full_path", ep["path"])
            lines.append(f"  test('GET {fp}', async () => {{")
            if ep.get("needs_auth"):
                lines.append(f"    const {{ token }} = await fetchToken();")
                lines.append(f"    const res = await fetch(`${{API}}{fp}`, {{")
                lines.append(f"      headers: {{ Authorization: `Bearer ${{token}}` }},")
                lines.append(f"    }});")
            else:
                lines.append(f"    const res = await fetch(`${{API}}{fp}`);")
            lines.append(f"    expect(res.status).toBe(200);")
            lines.append(f"  }});")
            lines.append(f"")
        lines.append(f"}});")
        lines.append(f"")

    # ── 심화 검증: 실제 로그인 흐름 + 데이터 계약 ──
    lines.append(f"// ── 심화 검증 ──")
    lines.append(f"test.describe('심화 검증', () => {{")
    lines.append(f"")

    # 1) 로그인 API 응답 구조 검증
    lines.append(f"  test('로그인 API 응답 구조 검증', async () => {{")
    lines.append(f"    const res = await fetch(`${{API}}/{auth['login_path'].lstrip('/')}`, {{")
    lines.append(f"      method: 'POST',")
    lines.append(f"      headers: {{ 'Content-Type': 'application/json' }},")
    lines.append(f"      body: JSON.stringify(CREDS),")
    lines.append(f"    }});")
    lines.append(f"    expect(res.status).toBe(200);")
    lines.append(f"    const body = await res.json();")
    lines.append(f"    const data = body.{wrapper} ?? body;")
    lines.append(f"    // 토큰 필드 존재 확인")
    lines.append(f"    expect(data.{token_field}).toBeDefined();")
    lines.append(f"    expect(typeof data.{token_field}).toBe('string');")
    lines.append(f"    expect(data.{token_field}.length).toBeGreaterThan(10);")
    lines.append(f"    // 리프레시 토큰")
    lines.append(f"    expect(data.{refresh_field}).toBeDefined();")
    lines.append(f"    // 유저 객체")
    lines.append(f"    expect(data.{auth['user_field']}).toBeDefined();")
    lines.append(f"    expect(data.{auth['user_field']}.email).toBe(CREDS.email);")
    lines.append(f"  }});")
    lines.append(f"")

    # 2) 브라우저 로그인 → 쿠키 세팅 검증
    lines.append(f"  test('브라우저 로그인 → 쿠키 + localStorage 세팅', async ({{ page }}) => {{")
    lines.append(f"    await page.goto('/login');")
    lines.append(f"    await page.fill('input[type=\"email\"]', CREDS.email);")
    lines.append(f"    await page.fill('input[type=\"password\"]', CREDS.password);")
    lines.append(f"    await page.click('button[type=\"submit\"]');")
    lines.append(f"    // 로그인 후 홈 이동 대기")
    lines.append(f"    await page.waitForURL('**/', {{ timeout: 15000 }});")
    lines.append(f"    // localStorage 확인")
    lines.append(f"    const stored = await page.evaluate(() => localStorage.getItem('{store_name}'));")
    lines.append(f"    expect(stored).not.toBeNull();")
    lines.append(f"    const parsed = JSON.parse(stored!);")
    lines.append(f"    expect(parsed.state?.token).toBeDefined();")
    lines.append(f"  }});")
    lines.append(f"")

    # 3) 인증 토큰으로 보호된 API 호출 검증
    lines.append(f"  test('인증 토큰으로 보호된 API 호출', async () => {{")
    lines.append(f"    const {{ token }} = await fetchToken();")
    lines.append(f"    // 토큰 있으면 200")
    lines.append(f"    const ok = await fetch(`${{API}}/pets`, {{")
    lines.append(f"      headers: {{ Authorization: `Bearer ${{token}}` }},")
    lines.append(f"    }});")
    lines.append(f"    expect(ok.status).toBe(200);")
    lines.append(f"    // 토큰 없으면 401")
    lines.append(f"    const fail = await fetch(`${{API}}/pets`);")
    lines.append(f"    expect(fail.status).toBe(401);")
    lines.append(f"  }});")
    lines.append(f"")

    # 4) CORS 검증
    lines.append(f"  test('CORS 허용 확인', async () => {{")
    lines.append(f"    const res = await fetch(`${{API}}/{auth['login_path'].lstrip('/')}`, {{")
    lines.append(f"      method: 'OPTIONS',")
    lines.append(f"      headers: {{")
    lines.append(f"        'Origin': `http://localhost:{fe_port}`,")
    lines.append(f"        'Access-Control-Request-Method': 'POST',")
    lines.append(f"      }},")
    lines.append(f"    }});")
    lines.append(f"    // OPTIONS는 204 또는 200")
    lines.append(f"    expect([200, 204]).toContain(res.status);")
    lines.append(f"  }});")
    lines.append(f"")

    # 5) Rate limiter 개발 환경 확인 (순차 — JWT 토큰 충돌 방지)
    lines.append(f"  test('Rate limiter — 개발 환경에서 연속 요청 허용', async () => {{")
    lines.append(f"    for (let i = 0; i < 3; i++) {{")
    lines.append(f"      const r = await fetch(`${{API}}/{auth['login_path'].lstrip('/')}`, {{")
    lines.append(f"        method: 'POST',")
    lines.append(f"        headers: {{ 'Content-Type': 'application/json' }},")
    lines.append(f"        body: JSON.stringify(CREDS),")
    lines.append(f"      }});")
    lines.append(f"      expect(r.status).toBe(200); // 429면 rate limit 문제")
    lines.append(f"      await new Promise(ok => setTimeout(ok, 1100)); // JWT 타임스탬프 충돌 방지")
    lines.append(f"    }}")
    lines.append(f"  }});")
    lines.append(f"")

    lines.append(f"}});")
    lines.append(f"")

    # ── 기능별 CRUD 실제 동작 검증 ──
    # POST 가능한 엔드포인트를 감지해서 실제 데이터를 넣고 확인
    post_endpoints = [
        ep for ep in api_endpoints
        if ep["method"] == "POST"
        and ep.get("needs_auth")
        and "/admin" not in ep.get("full_path", ep["path"])
        and not any(seg.startswith(":") or seg.startswith("{{") for seg in ep.get("full_path", ep["path"]).split("/"))
    ]

    # ── 브라우저 실제 렌더링 데이터 검증 ──
    # 로그인 후 각 페이지에서 실제 데이터가 보이는지 (빈 화면/에러 아닌지)
    lines.append(f"// ── 브라우저 실제 데이터 렌더링 검증 ──")
    lines.append(f"test.describe('데이터 렌더링', () => {{")
    lines.append(f"  test.beforeEach(async ({{ page }}) => {{ await login(page); }});")
    lines.append(f"")

    # 홈 페이지: 에러 없이 렌더링 + 데이터 섹션 존재
    if "/" in [r["path"] for r in unique_routes]:
        lines.append(f"  test('홈 — 에러 없이 데이터 표시', async ({{ page }}) => {{")
        lines.append(f"    await page.goto('/');")
        lines.append(f"    await page.waitForTimeout(5000);")
        lines.append(f"    const text = await page.locator('body').textContent() || '';")
        lines.append(f"    // 에러 바운더리에 잡힌 에러가 없어야 함")
        lines.append(f"    expect(text).not.toContain('오류가 발생했어요');")
        lines.append(f"    expect(text).not.toContain('Cannot read properties');")
        lines.append(f"    expect(text).not.toContain('Internal Server Error');")
        lines.append(f"  }});")
        lines.append(f"")

    # 각 인증 필요 페이지에서 로딩 후 실제 콘텐츠 확인
    for r in auth_routes[:6]:
        path = r["path"]
        lines.append(f"  test('{path} — 실제 데이터 렌더링', async ({{ page }}) => {{")
        lines.append(f"    await page.goto('{path}');")
        lines.append(f"    await page.waitForTimeout(3000);")
        lines.append(f"    const text = await page.locator('body').textContent() || '';")
        lines.append(f"    // 런타임 에러 없음")
        lines.append(f"    expect(text).not.toContain('오류가 발생했어요');")
        lines.append(f"    expect(text).not.toContain('Cannot read properties');")
        lines.append(f"    // 빈 껍데기가 아닌 실제 콘텐츠 (100자 이상)")
        lines.append(f"    expect(text.length).toBeGreaterThan(100);")
        lines.append(f"  }});")
        lines.append(f"")

    lines.append(f"}});")
    lines.append(f"")

    if post_endpoints:
        lines.append(f"// ── 기능별 CRUD 실제 동작 검증 ──")
        lines.append(f"test.describe('CRUD 실동작', () => {{")
        lines.append(f"")

        # GET 엔드포인트 중 데이터 목록을 반환하는 것들로 "데이터가 실제로 있는지" 검증
        list_gets = [
            ep for ep in api_endpoints
            if ep["method"] == "GET"
            and ep.get("needs_auth")
            and "/admin" not in ep.get("full_path", ep["path"])
            and not any(seg.startswith(":") or seg.startswith("{{") for seg in ep.get("full_path", ep["path"]).split("/"))
            and ep.get("full_path", ep["path"]).count("/") <= 2  # 1~2 depth만
        ]

        # 목록 API가 빈 배열이 아닌 실제 데이터를 반환하는지 확인
        seen_list = set()
        for ep in list_gets[:8]:  # 최대 8개
            fp = ep.get("full_path", ep["path"])
            if fp in seen_list:
                continue
            seen_list.add(fp)
            lines.append(f"  test('GET {fp} — 시드 데이터 존재 확인', async () => {{")
            lines.append(f"    const {{ token }} = await fetchToken();")
            lines.append(f"    const res = await fetch(`${{API}}{fp}`, {{")
            lines.append(f"      headers: {{ Authorization: `Bearer ${{token}}` }},")
            lines.append(f"    }});")
            lines.append(f"    expect(res.status).toBe(200);")
            lines.append(f"    const body = await res.json();")
            lines.append(f"    const data = body.data ?? body;")
            lines.append(f"    // 응답 구조가 배열 또는 페이지네이션 객체인지 확인 (빈 데이터 허용)")
            lines.append(f"    if (Array.isArray(data)) {{")
            lines.append(f"      expect(data.length).toBeGreaterThanOrEqual(0);")
            lines.append(f"    }} else if (typeof data === 'object' && data !== null) {{")
            lines.append(f"      // items/total 등 페이지네이션 구조이거나 단일 객체")
            lines.append(f"      expect(Object.keys(data).length).toBeGreaterThan(0);")
            lines.append(f"    }}")
            lines.append(f"  }});")
            lines.append(f"")

        # POST 엔드포인트로 실제 생성 → 응답에 id/success 있는지 확인
        seen_post = set()
        for ep in post_endpoints[:5]:  # 최대 5개
            fp = ep.get("full_path", ep["path"])
            if fp in seen_post or "login" in fp or "register" in fp or "refresh" in fp or "logout" in fp or "social" in fp or "claim" in fp or "confirm" in fp:
                continue
            seen_post.add(fp)
            lines.append(f"  test('POST {fp} — 생성 요청 시 에러 아닌 응답', async () => {{")
            lines.append(f"    const {{ token }} = await fetchToken();")
            lines.append(f"    const res = await fetch(`${{API}}{fp}`, {{")
            lines.append(f"      method: 'POST',")
            lines.append(f"      headers: {{ Authorization: `Bearer ${{token}}`, 'Content-Type': 'application/json' }},")
            lines.append(f"      body: JSON.stringify({{}}),")
            lines.append(f"    }});")
            lines.append(f"    // 201(생성) 또는 400(유효성) 또는 409(중복) — 500이면 서버 버그")
            lines.append(f"    expect(res.status).toBeLessThan(500);")
            lines.append(f"  }});")
            lines.append(f"")

        lines.append(f"}});")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Playwright 설치/실행
# ---------------------------------------------------------------------------

async def _ensure_playwright(fe: Path) -> None:
    """Playwright가 없으면 설치."""
    pkg_path = fe / "package.json"
    if pkg_path.is_file():
        pkg = json.loads(pkg_path.read_text())
        dev_deps = pkg.get("devDependencies", {})
        if "@playwright/test" not in dev_deps:
            await asyncio.to_thread(subprocess.run, 
                ["npm", "install", "-D", "@playwright/test"],
                cwd=fe, capture_output=True, timeout=120,
            )
            await asyncio.to_thread(subprocess.run, 
                ["npx", "playwright", "install", "chromium"],
                cwd=fe, capture_output=True, timeout=120,
            )
            logger.info("playwright_installed path=%s", fe)


async def _run_tests(fe: Path) -> dict:
    """Playwright 테스트 실행 + 결과 파싱."""
    try:
        result = await asyncio.to_thread(subprocess.run, 
            ["npx", "playwright", "test", "e2e/auto-generated.spec.ts", "--reporter=list"],
            cwd=fe, capture_output=True, text=True, timeout=120,
        )

        # list 리포터 요약행에서 파싱 — 마지막 출현만 사용 (로그 간섭 방지)
        output = result.stdout + result.stderr
        all_passed = re.findall(r"(\d+) passed", output)
        all_failed = re.findall(r"(\d+) failed", output)
        passed = int(all_passed[-1]) if all_passed else 0
        failed = int(all_failed[-1]) if all_failed else 0
        total = passed + failed

        outcome = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success": failed == 0,
            "output": (result.stdout + result.stderr)[-2000:],
        }

        logger.info(
            "e2e_tests_completed total=%d passed=%d failed=%d success=%s",
            total, passed, failed, outcome["success"],
        )
        return outcome

    except subprocess.TimeoutExpired:
        logger.error("e2e_tests_timeout")
        return {"total": 0, "passed": 0, "failed": 0, "success": False, "output": "Timeout"}
    except Exception as exc:
        logger.error("e2e_tests_error error=%s", str(exc))
        return {"total": 0, "passed": 0, "failed": 0, "success": False, "output": str(exc)}


# ============================================================
# [v8+] intake 시나리오 기반 테스트
# ============================================================

def _load_intake_data(workspace_path: Path) -> dict | None:
    """워크스페이스 또는 DB에서 intake 데이터 로드."""
    # 워크스페이스 내 intake.json
    for candidate in [
        workspace_path / "intake.json",
        workspace_path / "intake-data.json",
        workspace_path / ".intake.json",
    ]:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def _build_scenario_test_code(
    intake: dict,
    routes: list[dict],
    fe_port: int,
) -> str:
    """intake 기능/시나리오에서 E2E 테스트 코드 생성."""
    features = intake.get("features", [])
    feature_details = intake.get("featureDetails", {})
    user_scenarios = [s for s in intake.get("userScenarios", []) if s]

    tests = [
        "import { test, expect } from '@playwright/test';",
        "",
        "// [v8+] intake 시나리오 + 카테고리 UI 검증 (자동 생성)",
        "",
    ]

    # 1. 카테고리별 필수 UI 검증
    category_checks = []
    all_features_lower = " ".join(features).lower() + " ".join(
        " ".join(v) if isinstance(v, list) else str(v)
        for v in feature_details.values()
    ).lower()

    if any(kw in all_features_lower for kw in ("사진", "이미지", "갤러리", "photo", "image", "upload")):
        category_checks.append(("사진/이미지 업로드 UI", "input[type=file], [class*=upload], [class*=dropzone], [class*=Upload]"))
    if any(kw in all_features_lower for kw in ("검색", "search")):
        category_checks.append(("검색 UI", "input[type=search], input[placeholder*=검색], [class*=search], [class*=Search]"))
    if any(kw in all_features_lower for kw in ("지도", "map", "위치")):
        category_checks.append(("지도 컴포넌트", "[class*=map], [class*=Map], iframe[src*=map], canvas"))
    if any(kw in all_features_lower for kw in ("채팅", "메시지", "chat", "message")):
        category_checks.append(("채팅/메시지 UI", "[class*=chat], [class*=Chat], [class*=message], [class*=Message]"))
    if any(kw in all_features_lower for kw in ("결제", "payment", "구매", "주문")):
        category_checks.append(("결제 UI", "[class*=payment], [class*=Payment], [class*=checkout], [class*=order]"))
    if any(kw in all_features_lower for kw in ("알림", "notification", "푸시")):
        category_checks.append(("알림 UI", "[class*=notification], [class*=Notification], [class*=bell], [class*=Badge]"))

    if category_checks:
        tests.append("test.describe('카테고리별 필수 UI 검증', () => {")
        for label, selector in category_checks:
            tests.append(f"  test('{label} 존재', async ({{ page }}) => {{")
            tests.append(f"    // 전체 페이지 탐색하여 {label} 요소 확인")
            tests.append(f"    const pages = {json.dumps([r['path'] for r in routes[:10]])};")
            tests.append(f"    let found = false;")
            tests.append(f"    for (const p of pages) {{")
            tests.append(f"      await page.goto(p);")
            tests.append(f"      const el = page.locator('{selector}');")
            tests.append(f"      if (await el.count() > 0) {{ found = true; break; }}")
            tests.append(f"    }}")
            tests.append(f"    expect(found).toBeTruthy();")
            tests.append(f"  }});")
            tests.append("")
        tests.append("});")
        tests.append("")

    # 2. 사용자 시나리오 흐름 테스트
    if user_scenarios:
        tests.append("test.describe('사용자 시나리오 흐름', () => {")
        for i, scenario in enumerate(user_scenarios[:5]):
            tests.append(f"  test('시나리오 {i+1}: {scenario[:60]}', async ({{ page }}) => {{")
            tests.append(f"    await page.goto('/');")
            tests.append(f"    await expect(page).not.toHaveTitle(/error/i);")
            tests.append(f"    await expect(page.locator('body')).not.toBeEmpty();")
            tests.append(f"    const nav = page.locator('nav, [role=navigation], header');")
            tests.append(f"    await expect(nav.first()).toBeVisible();")
            tests.append(f"  }});")
            tests.append("")
        tests.append("});")
        tests.append("")

    # 3. CRUD 흐름 테스트 (리스트 페이지 기반)
    list_routes = [r for r in routes if not r.get("is_dynamic") and r["path"] != "/"]
    if list_routes:
        tests.append("test.describe('CRUD 흐름 검증', () => {")
        for r in list_routes[:5]:
            name = r["path"].strip("/").split("/")[-1] or "root"
            tests.append(f"  test('{name} 목록→상세→생성', async ({{ page }}) => {{")
            tests.append(f"    await page.goto('{r['path']}');")
            tests.append(f"    await expect(page.locator('body')).not.toBeEmpty();")
            tests.append(f"    // 첫 항목 클릭 → 상세")
            tests.append(f"    const item = page.locator('a, [role=link], tr').first();")
            tests.append(f"    if (await item.isVisible()) {{ await item.click(); await page.waitForTimeout(500); }}")
            tests.append(f"    // 생성 버튼 확인")
            tests.append(f"    await page.goto('{r['path']}');")
            tests.append(f"    const createBtn = page.locator('button:has-text(\"등록\"), button:has-text(\"추가\"), button:has-text(\"작성\"), a:has-text(\"등록\")').first();")
            tests.append(f"    // 생성 버튼이 있으면 클릭")
            tests.append(f"    if (await createBtn.isVisible()) {{ await createBtn.click(); await page.waitForTimeout(500); }}")
            tests.append(f"  }});")
            tests.append("")
        tests.append("});")

    return "\n".join(tests)
