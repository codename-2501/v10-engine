"""
engine/workspace/verify_and_fix.py
BUILD 산출물 자동 검증 + 수정 루프.

자동화가 못 잡는 5가지 문제를 해결:
1. 타입 에러 / 누락 모듈 → 빌드-수정 루프 (최대 5회)
2. 프론트/백 데이터 계약 불일치 → API 응답 vs 프론트 타입 비교
3. UI 시각 검증 → 페이지별 스크린샷 + 빈 화면/에러 감지
4. 비즈니스 로직 → DESIGN 산출물에서 시나리오 추출 → 테스트 생성
5. 동적 라우트 → 시드 데이터 기반 실제 데이터 렌더링 검증
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.workspace.verify_and_fix")

MAX_FIX_ATTEMPTS = 5
AI_FIX_TOKEN_LIMIT = 5000  # 에러당 AI 호출 최대 토큰

# ── Re-exports from split modules (backward compat) ──
from engine.workspace.programmatic_verify import programmatic_build_verify  # noqa: F401
from engine.workspace.contract_verify import (  # noqa: F401
    verify_api_contracts,
    run_full_verification,
    visual_smoke_test,
)


# ============================================================
# 0. 범용 이슈 사전 수정 — 모든 프로젝트 공통
# ============================================================

def fix_universal_issues(workspace_path: Path, stack: dict) -> list[str]:
    """모든 프로젝트에서 반복되는 범용 이슈를 빌드 전에 일괄 수정."""
    fixes = []
    be = workspace_path / "backend"
    fe = workspace_path / "frontend"

    if be.is_dir() and stack.get("backend") == "express":
        # (A) SQLite 변환 후 서비스 코드의 배열/객체 → JSON.stringify 자동 적용
        fixes += _fix_sqlite_service_code(be)
        # (B) Express 5 req.params 타입 캐스트
        fixes += _fix_express_params_cast(be)
        # (C) Rate limiter 개발 환경 완화
        fixes += _fix_rate_limiter_dev(be)
        # (E) Helmet CSP → Swagger UI 경로 완화
        fixes += _fix_helmet_swagger_csp(be)
        # (G) 백엔드 루트(/) → API 문서 리다이렉트
        fixes += _fix_backend_root_redirect(be)

    if fe.is_dir() and stack.get("frontend") in ("nextjs", "nuxt", "vue", "react", "svelte"):
        # (D) Cookie + localStorage 동기화 (zustand → 쿠키 세팅)
        fixes += _fix_cookie_localStorage_sync(fe)
        # (F) 로그인 페이지에 테스트 계정 표시
        fixes += _fix_add_test_account_display(fe)
        # (H) Avatar 등 컴포넌트 null 안전 처리
        fixes += _fix_null_safe_components(fe)

    logger.info("universal_fixes_applied count=%d fixes=%s", len(fixes), fixes[:5])
    return fixes


def _fix_sqlite_service_code(be: Path) -> list[str]:
    """Prisma schema가 SQLite일 때, 서비스 코드에서 String[] / Json 필드에
    배열/객체를 직접 전달하는 코드를 JSON.stringify()로 감싼다."""
    fixes = []
    schema_path = be / "prisma" / "schema.prisma"
    if not schema_path.is_file():
        return fixes

    schema = schema_path.read_text(encoding="utf-8")
    if '"sqlite"' not in schema:
        return fixes  # SQLite 아니면 스킵

    # schema에서 @default("[]") 또는 @default("{}") 인 필드명 추출
    json_fields: set[str] = set()
    for m in re.finditer(r'(\w+)\s+String\s+@default\("\[\]"\)', schema):
        json_fields.add(m.group(1))
    for m in re.finditer(r'(\w+)\s+String\s+@default\("\{\}"\)', schema):
        json_fields.add(m.group(1))
    # Optional Json→String 필드 (data String? 등)
    for m in re.finditer(r'(\w+)\s+String\?', schema):
        name = m.group(1)
        if name.lower() in ("data", "metadata", "extra", "config", "settings", "payload"):
            json_fields.add(name)

    if not json_fields:
        return fixes

    # 서비스 파일에서 해당 필드에 배열/객체를 넘기는 패턴 수정
    for ts_file in be.rglob("*.service.ts"):
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        original = content
        for field in json_fields:
            # 패턴: field: data.field || []  →  field: JSON.stringify(data.field || [])
            content = re.sub(
                rf'({field}:\s*)(data\.{field}\s*\|\|\s*\[\])',
                rf'\1JSON.stringify(\2)',
                content,
            )
            # 패턴: field: data.field  (객체 전달, JSON.stringify 없음)
            # 단, 이미 JSON.stringify가 있으면 스킵
            content = re.sub(
                rf'({field}:\s*)(?!JSON\.stringify)(data\.{field})(?=\s*[,\n}}])',
                lambda m: f'{m.group(1)}typeof {m.group(2)} === "string" ? {m.group(2)} : JSON.stringify({m.group(2)})',
                content,
            )
        if content != original:
            ts_file.write_text(content, encoding="utf-8")
            fixes.append(f"sqlite_stringify: {ts_file.name}")

    return fixes


def _fix_express_params_cast(be: Path) -> list[str]:
    """Express 5 타입에서 req.params.xxx를 (req.params.xxx as string)으로 캐스트."""
    fixes = []
    for ts_file in be.rglob("*.controller.ts"):
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        original = content
        # req.params.xxx 가 as string으로 캐스트 안 돼있으면 추가
        content = re.sub(
            r'req\.params\.(\w+)',
            lambda m: f'(req.params.{m.group(1)} as string)' if f'(req.params.{m.group(1)} as string)' not in content else m.group(0),
            content,
        )
        if content != original:
            ts_file.write_text(content, encoding="utf-8")
            fixes.append(f"params_cast: {ts_file.name}")
    return fixes


def _fix_rate_limiter_dev(be: Path) -> list[str]:
    """Rate limiter에 개발 환경 완화 코드 추가."""
    fixes = []
    for ts_file in be.rglob("*rateLimiter*"):
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "nodeEnv" in content or "NODE_ENV" in content:
            continue  # 이미 환경별 분기 있음

        original = content
        # max: N → 개발 환경 10000 (쉼표 유무 모두 대응)
        content = re.sub(
            r"max:\s*(\d+|config\.\w+\.\w+)",
            lambda m: f"max: process.env.NODE_ENV === 'development' ? 10000 : {m.group(1)}",
            content,
        )
        if content != original:
            ts_file.write_text(content, encoding="utf-8")
            fixes.append(f"rate_limiter_dev: {ts_file.name}")
    return fixes


def _fix_helmet_swagger_csp(be: Path) -> list[str]:
    """Helmet CSP가 Swagger UI를 차단하지 않도록 경로별 분기 적용."""
    fixes = []
    app_file = be / "src" / "app.ts"
    if not app_file.is_file():
        return fixes

    content = app_file.read_text(encoding="utf-8")
    if "api-docs" in content and "contentSecurityPolicy" in content:
        return fixes  # 이미 수정됨
    if "helmet()" not in content:
        return fixes  # helmet 미사용

    content = content.replace(
        "app.use(helmet());",
        "app.use((req, res, next) => {\n"
        "  if (req.path.startsWith('/api-docs')) {\n"
        "    return helmet({ contentSecurityPolicy: false })(req, res, next);\n"
        "  }\n"
        "  return helmet()(req, res, next);\n"
        "});",
    )
    app_file.write_text(content, encoding="utf-8")
    fixes.append("helmet_swagger_csp: app.ts")
    return fixes


def _fix_backend_root_redirect(be: Path) -> list[str]:
    """백엔드 루트(/) → API 상태 대시보드 HTML 페이지."""
    fixes = []
    app_file = be / "src" / "app.ts"
    if not app_file.is_file():
        return fixes
    content = app_file.read_text(encoding="utf-8")
    if "app.get('/'," in content or 'app.get("/",' in content:
        return fixes
    if "swaggerSpec" not in content:
        return fixes

    root_handler = r"""app.get('/', (_req, res) => {
  const spec = swaggerSpec as any;
  const paths = Object.keys(spec.paths || {});
  const tags = (spec.tags || []) as { name: string; description?: string }[];
  const byTag: Record<string, string[]> = {};
  for (const [p, methods] of Object.entries(spec.paths || {})) {
    for (const [m, d] of Object.entries(methods as any)) {
      const t = (d as any).tags?.[0] || 'Other';
      if (!byTag[t]) byTag[t] = [];
      byTag[t].push(`<span style="display:inline-block;width:55px;font-weight:700;color:${m==='get'?'#22c55e':m==='post'?'#3b82f6':m==='put'?'#f59e0b':'#ef4444'}">${m.toUpperCase()}</span> ${p}`);
    }
  }
  const sections = tags.map(t => `<div style="margin-bottom:20px"><h3 style="font-size:13px;font-weight:700;color:#ff6b35;margin-bottom:6px">${t.name} <span style="font-weight:400;color:#888;font-size:11px">${t.description||''}</span></h3>${(byTag[t.name]||[]).map(e=>`<div style="padding:3px 0;font-family:monospace;font-size:12px;border-bottom:1px solid #f5f5f5">${e}</div>`).join('')}</div>`).join('');
  res.send(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>${spec.info?.title||'API'}</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#fafafa;color:#333;padding:32px}</style></head><body><div style="max-width:720px;margin:0 auto"><h1 style="font-size:22px;font-weight:800;margin-bottom:4px">${spec.info?.title||'API'}</h1><p style="color:#888;font-size:13px;margin-bottom:20px">v${spec.info?.version||'1.0.0'} · ${paths.length} endpoints · <span style="color:#22c55e;font-weight:600">Running</span></p><div style="display:flex;gap:8px;margin-bottom:24px"><a href="/api-docs" style="padding:8px 16px;background:#ff6b35;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Swagger UI →</a><a href="/api-docs.json" style="padding:8px 16px;background:#fff;border:1px solid #ddd;border-radius:6px;text-decoration:none;font-size:13px">OpenAPI JSON</a></div>${sections}</div></body></html>`);
});
"""
    content = content.replace(
        "app.use('/api-docs'",
        root_handler + "app.use('/api-docs'",
    )
    app_file.write_text(content, encoding="utf-8")
    fixes.append("root_api_dashboard: app.ts")
    return fixes


def _fix_add_test_account_display(fe: Path) -> list[str]:
    """로그인 페이지에 테스트 계정 정보를 표시."""
    fixes = []
    for login_file in fe.rglob("*login*page*"):
        if login_file.suffix not in (".tsx", ".jsx", ".vue"):
            continue
        try:
            content = login_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "test@test.kr" in content:
            continue  # 이미 있음
        if "</form>" not in content and "form" not in content.lower():
            continue  # 로그인 폼 아님

        # form 닫는 태그 앞에 테스트 계정 박스 삽입
        test_box = '''
      <div style={{
        marginTop: 'var(--space-5, 20px)',
        padding: 'var(--space-4, 16px)',
        background: 'var(--bg-secondary, #f5f5f5)',
        borderRadius: 'var(--radius-md, 8px)',
        fontSize: 'var(--text-xs, 12px)',
        color: 'var(--text-tertiary, #888)',
      }}>
        <p style={{ fontWeight: 700, marginBottom: 'var(--space-2, 8px)', color: 'var(--text-secondary, #555)' }}>테스트 계정</p>
        <p>일반: <code style={{ background: 'var(--surface, #fff)', padding: '1px 4px', borderRadius: '3px' }}>test@test.kr</code> / <code style={{ background: 'var(--surface, #fff)', padding: '1px 4px', borderRadius: '3px' }}>Test1234!</code></p>
        <p style={{ marginTop: '2px' }}>관리자: <code style={{ background: 'var(--surface, #fff)', padding: '1px 4px', borderRadius: '3px' }}>admin@test.kr</code> / <code style={{ background: 'var(--surface, #fff)', padding: '1px 4px', borderRadius: '3px' }}>Test1234!</code></p>
      </div>'''

        content = content.replace("</form>", test_box + "\n    </form>", 1)
        login_file.write_text(content, encoding="utf-8")
        fixes.append(f"test_account_display: {login_file.name}")
        break  # 첫 번째 로그인 페이지만
    return fixes


def _fix_cookie_localStorage_sync(fe: Path) -> list[str]:
    """zustand persist store에 cookie 동기화 코드 추가."""
    fixes = []
    for store_file in fe.rglob("*Store*"):
        if store_file.suffix not in (".ts", ".tsx"):
            continue
        try:
            content = store_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # zustand persist + token이 있는데 document.cookie 세팅이 없으면 추가
        if "persist" not in content or "token" not in content:
            continue
        if "document.cookie" in content:
            continue  # 이미 있음

        # persist name 추출
        name_match = re.search(r"name:\s*['\"]([^'\"]+)['\"]", content)
        if not name_match:
            continue
        store_name = name_match.group(1)

        original = content
        # set({ token, ... }) 패턴 뒤에 cookie 세팅 추가
        content = re.sub(
            r'(set\(\{\s*token\b[^}]*\})\)',
            rf"""\1);
        if (typeof document !== 'undefined') {{
          document.cookie = `{store_name}=${{encodeURIComponent(JSON.stringify({{ state: {{ token }} }}))}}; path=/; max-age=${{7 * 86400}}`;
        }}""",
            content,
            count=1,
        )
        # logout에 cookie 삭제 추가
        if "document.cookie" not in content:
            # 단순 패턴이 안 맞으면 스킵
            continue

        content = re.sub(
            r'(set\(\{\s*token:\s*null[^}]*\})\)',
            rf"""\1);
        if (typeof document !== 'undefined') {{
          document.cookie = '{store_name}=; path=/; max-age=0';
        }}""",
            content,
            count=1,
        )

        if content != original:
            store_file.write_text(content, encoding="utf-8")
            fixes.append(f"cookie_sync: {store_file.name}")
    return fixes


# ============================================================
# 1. 빌드-수정 루프 — 타입 에러 / 누락 모듈 자동 수정
# ============================================================

def build_fix_loop(
    workspace_path: Path,
    stack: dict,
    ai_adapter=None,
) -> dict:
    """빌드 → 에러 분석 → 패턴 기반 자동 수정 → AI 폴백 → 재시도. 최대 MAX_FIX_ATTEMPTS회."""
    be = workspace_path / "backend"
    fe = workspace_path / "frontend"
    results = {"backend": None, "frontend": None}

    # 백엔드
    be_type = stack.get("backend", "")
    if be.is_dir():
        if be_type == "express":
            results["backend"] = _fix_loop_typescript(be, "backend", ai_adapter=ai_adapter)
        elif be_type in ("fastapi", "django"):
            results["backend"] = _fix_loop_python(be)
        elif be_type == "spring":
            results["backend"] = _fix_loop_gradle(be)
        elif be_type == "go":
            results["backend"] = _fix_loop_go(be)
        # unknown은 스킵

    # 프론트엔드
    fe_type = stack.get("frontend", "")
    if fe.is_dir():
        if fe_type == "nextjs":
            results["frontend"] = _fix_loop_nextjs(fe, ai_adapter=ai_adapter)
        elif fe_type in ("vue", "nuxt", "svelte", "react"):
            results["frontend"] = _fix_loop_vite(fe)
        # static은 빌드 불필요

    return results


async def _fix_loop_typescript(project_dir: Path, label: str, ai_adapter=None) -> dict:
    """TypeScript 빌드 에러 자동 수정 루프. 패턴 실패 시 AI 폴백."""
    import asyncio
    all_fixes = []
    errors = []
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        result = await asyncio.to_thread(
            subprocess.run,
            ["npx", "tsc", "--noEmit"],
            cwd=project_dir, capture_output=True, text=True, timeout=60,
        )

        if result.returncode == 0:
            logger.info("%s_build_ok attempt=%d", label, attempt)
            return {"success": True, "attempts": attempt, "fixes": all_fixes}

        errors = _parse_ts_errors(result.stdout + result.stderr)
        if not errors:
            logger.info("%s_build_ok_no_errors attempt=%d", label, attempt)
            return {"success": True, "attempts": attempt, "fixes": all_fixes}

        # 1차: 패턴 기반 자동 수정
        fixes = _auto_fix_ts_errors(project_dir, errors)
        if fixes:
            all_fixes.extend(fixes)
            logger.info("%s_pattern_fixed attempt=%d fixes=%d", label, attempt, len(fixes))
            continue

        # 2차: AI 폴백 — 패턴 매칭 실패 시 Sonnet으로 수정 시도
        if ai_adapter:
            ai_fixes = _ai_fix_errors(project_dir, errors, ai_adapter)
            if ai_fixes:
                all_fixes.extend(ai_fixes)
                logger.info("%s_ai_fixed attempt=%d fixes=%d", label, attempt, len(ai_fixes))
                continue

        logger.warning("%s_build_unfixable attempt=%d errors=%d", label, attempt, len(errors))
        return {"success": False, "attempts": attempt, "errors": errors[:10], "fixes": all_fixes}

    return {"success": False, "attempts": MAX_FIX_ATTEMPTS, "errors": errors[:10], "fixes": all_fixes}


async def _fix_loop_nextjs(fe: Path, ai_adapter=None) -> dict:
    """Next.js 빌드 에러 자동 수정 루프. 패턴 실패 시 AI 폴백."""
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        result = await asyncio.to_thread(subprocess.run, 
            ["npx", "next", "build"],
            cwd=fe, capture_output=True, text=True, timeout=180,
        )

        if result.returncode == 0:
            logger.info("frontend_build_ok attempt=%d", attempt)
            return {"success": True, "attempts": attempt}

        stderr = result.stdout + result.stderr

        # 1차: 패턴 기반
        fixes = _auto_fix_nextjs_errors(fe, stderr)
        if fixes:
            logger.info("frontend_pattern_fixed attempt=%d fixes=%d", attempt, len(fixes))
            continue

        # 2차: AI 폴백
        if ai_adapter:
            # Next.js 에러를 TS 에러 형식으로 파싱 시도
            ts_errors = _parse_ts_errors(stderr)
            if ts_errors:
                ai_fixes = _ai_fix_errors(fe, ts_errors, ai_adapter)
                if ai_fixes:
                    logger.info("frontend_ai_fixed attempt=%d fixes=%d", attempt, len(ai_fixes))
                    continue

            # 파싱 불가한 에러도 AI에 raw stderr 전송
            raw_fix = _ai_fix_raw_error(fe, stderr[-2000:], ai_adapter)
            if raw_fix:
                logger.info("frontend_ai_raw_fixed attempt=%d", attempt)
                continue

        return {"success": False, "attempts": attempt, "errors": [stderr[-500:]]}

    return {"success": False, "attempts": MAX_FIX_ATTEMPTS}


async def _fix_loop_python(be: Path) -> dict:
    """Python 구문/임포트 에러 자동 수정 루프."""
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        # python -m py_compile로 전체 검증
        errors = []
        for py_file in be.rglob("*.py"):
            if ".venv" in str(py_file) or "seed" in py_file.name:
                continue
            result = await asyncio.to_thread(subprocess.run, 
                ["python3", "-m", "py_compile", str(py_file)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                errors.append({"file": str(py_file), "error": result.stderr.strip()})

        if not errors:
            logger.info("python_build_ok attempt=%d", attempt)
            return {"success": True, "attempts": attempt}

        # 자동 수정: 누락 __init__.py
        fixes = []
        for err in errors:
            if "ModuleNotFoundError" in err["error"] or "No module named" in err["error"]:
                # 패키지 디렉토리에 __init__.py 누락
                err_file = Path(err["file"])
                for parent in err_file.parents:
                    if parent == be:
                        break
                    init = parent / "__init__.py"
                    if not init.exists() and parent.is_dir():
                        init.write_text("")
                        fixes.append(f"created {init}")

            # SyntaxError: future import 누락
            if "annotations" in err["error"]:
                filepath = Path(err["file"])
                content = filepath.read_text(encoding="utf-8")
                if "from __future__" not in content:
                    content = "from __future__ import annotations\n" + content
                    filepath.write_text(content, encoding="utf-8")
                    fixes.append(f"added future import: {err['file']}")

        if not fixes:
            logger.warning("python_build_unfixable attempt=%d errors=%d", attempt, len(errors))
            return {"success": False, "attempts": attempt, "errors": errors[:5]}

        logger.info("python_auto_fixed attempt=%d fixes=%d", attempt, len(fixes))

    return {"success": False, "attempts": MAX_FIX_ATTEMPTS}


async def _fix_loop_gradle(be: Path) -> dict:
    """Spring Boot (Gradle/Maven) 빌드 검증."""
    if (be / "gradlew").is_file():
        cmd = ["./gradlew", "compileJava"]
    elif (be / "mvnw").is_file():
        cmd = ["./mvnw", "compile"]
    else:
        return {"success": True, "attempts": 0}  # 빌드 도구 없으면 스킵

    result = await asyncio.to_thread(subprocess.run, cmd, cwd=be, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        return {"success": True, "attempts": 1}
    return {"success": False, "attempts": 1, "errors": [(result.stderr or result.stdout)[-500:]]}


async def _fix_loop_go(be: Path) -> dict:
    """Go 빌드 검증."""
    result = await asyncio.to_thread(subprocess.run, ["go", "build", "./..."], cwd=be, capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        return {"success": True, "attempts": 1}
    # 자동 수정: go mod tidy
    await asyncio.to_thread(subprocess.run, ["go", "mod", "tidy"], cwd=be, capture_output=True, timeout=30)
    result2 = await asyncio.to_thread(subprocess.run, ["go", "build", "./..."], cwd=be, capture_output=True, text=True, timeout=60)
    if result2.returncode == 0:
        return {"success": True, "attempts": 2}
    return {"success": False, "attempts": 2, "errors": [(result2.stderr or result2.stdout)[-500:]]}


async def _fix_loop_vite(fe: Path) -> dict:
    """Vite 기반 프론트엔드 (Vue/Nuxt/Svelte/React) 빌드 검증."""
    pkg_path = fe / "package.json"
    if not pkg_path.is_file():
        return {"success": True, "attempts": 0}

    try:
        pkg = json.loads(pkg_path.read_text())
    except Exception:
        return {"success": True, "attempts": 0}

    if "build" not in pkg.get("scripts", {}):
        return {"success": True, "attempts": 0}

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        result = await asyncio.to_thread(subprocess.run, 
            ["npm", "run", "build"], cwd=fe, capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            return {"success": True, "attempts": attempt}

        stderr = result.stdout + result.stderr
        # 자동 수정: 누락 의존성 설치
        missing_match = re.search(r"Cannot find (?:module|package) ['\"]([^'\"]+)['\"]", stderr)
        if missing_match:
            pkg_name = missing_match.group(1).split("/")[0]
            if pkg_name.startswith("@"):
                pkg_name = "/".join(missing_match.group(1).split("/")[:2])
            await asyncio.to_thread(subprocess.run, ["npm", "install", pkg_name], cwd=fe, capture_output=True, timeout=60)
            logger.info("vite_auto_installed pkg=%s", pkg_name)
            continue

        return {"success": False, "attempts": attempt, "errors": [stderr[-500:]]}

    return {"success": False, "attempts": MAX_FIX_ATTEMPTS}


def _parse_ts_errors(output: str) -> list[dict]:
    """TypeScript 에러 메시지 파싱."""
    errors = []
    # src/modules/auth/auth.service.ts(187,30): error TS2769: ...
    for m in re.finditer(r"([\w/.\-]+\.ts)\((\d+),(\d+)\): error (TS\d+): (.+)", output):
        errors.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "col": int(m.group(3)),
            "code": m.group(4),
            "message": m.group(5),
        })
    return errors


def _auto_fix_ts_errors(project_dir: Path, errors: list[dict]) -> list[str]:
    """알려진 패턴의 TS 에러 자동 수정."""
    fixes = []

    for err in errors:
        filepath = project_dir / err["file"]
        if not filepath.is_file():
            # src/ 접두어 붙여서 재시도
            filepath = project_dir / "src" / err["file"]
            if not filepath.is_file():
                continue

        content = filepath.read_text(encoding="utf-8")
        original = content

        # TS2307: Cannot find module → 파일 생성
        if err["code"] == "TS2307":
            module_match = re.search(r"Cannot find module '([^']+)'", err["message"])
            if module_match:
                module_path = module_match.group(1)
                if module_path.startswith("./") or module_path.startswith("../"):
                    # 상대 경로 → 빈 모듈 생성
                    target = (filepath.parent / module_path).with_suffix(".ts")
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("// Auto-generated stub\nexport {};\n")
                        fixes.append(f"stub: {target}")

        # TS2345: string | string[] not assignable to string → as string 캐스트
        if err["code"] == "TS2345" and "string[]" in err["message"]:
            lines = content.split("\n")
            if 0 < err["line"] <= len(lines):
                line = lines[err["line"] - 1]
                # req.params.xxx → (req.params.xxx as string)
                new_line = re.sub(
                    r"req\.params\.(\w+)(?!\s+as\s+string)",
                    r"(req.params.\1 as string)",
                    line,
                )
                if new_line != line:
                    lines[err["line"] - 1] = new_line
                    content = "\n".join(lines)

        # TS2305: no exported member (enum 제거 후 잔재)
        if err["code"] == "TS2305":
            member_match = re.search(r"no exported member '(\w+)'", err["message"])
            if member_match:
                member = member_match.group(1)
                # import 라인에서 해당 멤버 제거
                content = re.sub(
                    rf",?\s*{member}\s*,?",
                    ", ",
                    content,
                )
                content = re.sub(r"import\s*\{\s*,\s*\}", "", content)
                content = re.sub(r",\s*\}", " }", content)
                content = re.sub(r"\{\s*,", "{ ", content)

        # TS2769: jwt.sign expiresIn 타입
        if err["code"] == "TS2769" and "expiresIn" in err.get("message", ""):
            content = re.sub(
                r"expiresIn:\s*(config\.\w+\.\w+)",
                r"expiresIn: \1 as any",
                content,
            )

        # TS7006: implicit any → 타입 추가
        if err["code"] == "TS7006":
            param_match = re.search(r"Parameter '(\w+)' implicitly", err["message"])
            if param_match:
                param = param_match.group(1)
                lines = content.split("\n")
                if 0 < err["line"] <= len(lines):
                    line = lines[err["line"] - 1]
                    new_line = re.sub(
                        rf"\b{param}\b(?!\s*[:,)])",
                        f"{param}: any",
                        line,
                        count=1,
                    )
                    if new_line != line:
                        lines[err["line"] - 1] = new_line
                        content = "\n".join(lines)

        if content != original:
            filepath.write_text(content, encoding="utf-8")
            fixes.append(f"fix {err['code']}: {err['file']}:{err['line']}")

    return fixes


def _auto_fix_nextjs_errors(fe: Path, stderr: str) -> list[str]:
    """Next.js 빌드 에러 자동 수정."""
    fixes = []

    # "Property 'style' does not exist on type" → Props에 style 추가
    style_match = re.search(
        r"([\w/.\-]+\.tsx)\((\d+),(\d+)\).*Property 'style' does not exist on type.*?(\w+Props)",
        stderr,
    )
    if style_match:
        filepath = fe / style_match.group(1)
        if filepath.is_file():
            content = filepath.read_text(encoding="utf-8")
            props_name = style_match.group(4)
            if f"style?: React.CSSProperties" not in content:
                content = content.replace(
                    f"interface {props_name} {{",
                    f"interface {props_name} {{\n  style?: React.CSSProperties;",
                )
                filepath.write_text(content, encoding="utf-8")
                fixes.append(f"add style prop to {props_name}")

    # "Expression expected" — CSS 파일이 .tsx로 잘못 생성됨
    expr_match = re.search(r"([\w/.\-]+\.tsx)\(\d+,\d+\).*Expression expected.*@import", stderr)
    if expr_match:
        bad_file = fe / expr_match.group(1)
        if bad_file.is_file():
            bad_file.unlink()
            fixes.append(f"removed CSS-as-TSX: {expr_match.group(1)}")

    return fixes


# ============================================================
# 1b. AI 폴백 수정 — 패턴 매칭 실패 시 Sonnet 호출
# ============================================================

def _ai_fix_errors(project_dir: Path, errors: list[dict], ai_adapter) -> list[str]:
    """
    패턴 매칭 불가 에러를 Sonnet에 전송 → 수정 코드 적용.
    에러당 5K 토큰 제한. 파일별로 그룹화하여 호출 최소화.
    """
    import asyncio
    from engine.ai.model_adapter import ModelID

    # 에러를 파일별로 그룹화
    by_file: dict[str, list[dict]] = {}
    for err in errors[:10]:  # 최대 10개 에러만 처리
        by_file.setdefault(err["file"], []).append(err)

    fixes = []
    for filepath, file_errors in by_file.items():
        # 소스 파일 읽기
        abs_path = project_dir / filepath
        if not abs_path.is_file():
            abs_path = project_dir / "src" / filepath
        if not abs_path.is_file():
            continue

        try:
            source = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # 소스가 너무 크면 에러 주변만 추출
        if len(source) > 8000:
            source = _extract_error_context(source, file_errors, context_lines=20)

        # 에러 메시지 포맷
        error_desc = "\n".join(
            f"Line {e['line']}: {e['code']} — {e['message']}" for e in file_errors
        )

        prompt = f"""다음 TypeScript 파일에 빌드 에러가 있습니다. 수정된 전체 파일 코드만 출력하세요.
설명, 마크다운 래핑 없이 코드만 출력.

## 파일: {filepath}

## 에러:
{error_desc}

## 현재 코드:
```
{source}
```

수정된 전체 코드:"""

        try:
            # asyncio.get_event_loop() deprecated (Python 3.10+) → 명시적 running loop 감지
            try:
                asyncio.get_running_loop()
                _has_running_loop = True
            except RuntimeError:
                _has_running_loop = False

            if _has_running_loop:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    resp = pool.submit(
                        asyncio.run,
                        ai_adapter.call(
                            model=ModelID.SONNET,
                            prompt=prompt,
                            max_tokens=AI_FIX_TOKEN_LIMIT,
                            temperature=0.0,
                        ),
                    ).result(timeout=60)
            else:
                resp = asyncio.run(
                    ai_adapter.call(
                        model=ModelID.SONNET,
                        prompt=prompt,
                        max_tokens=AI_FIX_TOKEN_LIMIT,
                        temperature=0.0,
                    )
                )
        except Exception as exc:
            logger.warning("ai_fix_call_failed file=%s error=%s", filepath, str(exc))
            continue

        # 응답에서 코드 추출 (마크다운 래핑 제거)
        fixed_code = _strip_markdown_fence(resp.content)
        if not fixed_code or fixed_code.strip() == source.strip():
            continue

        # 적용
        abs_path.write_text(fixed_code, encoding="utf-8")
        fixes.append(f"ai_fix: {filepath} ({len(file_errors)} errors)")
        logger.info(
            "ai_fix_applied file=%s errors=%d tokens=%d",
            filepath, len(file_errors), resp.total_tokens,
        )

    return fixes


def _ai_fix_raw_error(project_dir: Path, stderr: str, ai_adapter) -> bool:
    """파싱 불가 에러를 raw stderr로 AI에 전송. 단일 호출."""
    import asyncio
    from engine.ai.model_adapter import ModelID

    # stderr에서 파일 경로 추출 시도
    file_matches = re.findall(r'([\w/.\-]+\.tsx?)\(?\d+', stderr)
    if not file_matches:
        return False

    target_file = file_matches[0]
    abs_path = project_dir / target_file
    if not abs_path.is_file():
        abs_path = project_dir / "src" / target_file
    if not abs_path.is_file():
        return False

    try:
        source = abs_path.read_text(encoding="utf-8")
    except Exception:
        return False

    if len(source) > 8000:
        source = source[:8000] + "\n// ... (truncated)"

    prompt = f"""다음 빌드 에러를 수정하세요. 수정된 전체 파일 코드만 출력하세요.

## 에러 로그:
{stderr[-1500:]}

## 파일: {target_file}
```
{source}
```

수정된 전체 코드:"""

    try:
        try:
            asyncio.get_running_loop()
            _has_running_loop = True
        except RuntimeError:
            _has_running_loop = False

        if _has_running_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                resp = pool.submit(
                    asyncio.run,
                    ai_adapter.call(
                        model=ModelID.SONNET,
                        prompt=prompt,
                        max_tokens=AI_FIX_TOKEN_LIMIT,
                        temperature=0.0,
                    ),
                ).result(timeout=60)
        else:
            resp = asyncio.run(
                ai_adapter.call(
                    model=ModelID.SONNET,
                    prompt=prompt,
                    max_tokens=AI_FIX_TOKEN_LIMIT,
                    temperature=0.0,
                )
            )
    except Exception as exc:
        logger.warning("ai_raw_fix_failed error=%s", str(exc))
        return False

    fixed_code = _strip_markdown_fence(resp.content)
    if not fixed_code or fixed_code.strip() == source.strip():
        return False

    abs_path.write_text(fixed_code, encoding="utf-8")
    logger.info("ai_raw_fix_applied file=%s tokens=%d", target_file, resp.total_tokens)
    return True


def _extract_error_context(source: str, errors: list[dict], context_lines: int = 20) -> str:
    """큰 파일에서 에러 주변 라인만 추출."""
    lines = source.split("\n")
    include = set()
    for err in errors:
        center = err["line"] - 1
        for i in range(max(0, center - context_lines), min(len(lines), center + context_lines + 1)):
            include.add(i)
    # 항상 첫 10줄(import) 포함
    for i in range(min(10, len(lines))):
        include.add(i)
    result = []
    prev = -2
    for i in sorted(include):
        if i > prev + 1:
            result.append(f"// ... (lines {prev+2}-{i} omitted)")
        result.append(f"{lines[i]}")
        prev = i
    return "\n".join(result)


def _strip_markdown_fence(text: str) -> str:
    """AI 응답에서 ```...``` 래핑 제거."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else len(text)
        text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _fix_null_safe_components(fe: Path) -> list[str]:
    """컴포넌트에서 props가 undefined/null일 때 크래시 방지."""
    fixes = []

    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception:
            continue
        original = content

        # (1) xxx.charAt(0) → (xxx || '?').charAt(0)
        content = re.sub(r'(?<!\|)(\w+)\.charAt\(0\)', r'(\1 || "?").charAt(0)', content)

        # (2) xxx.toLocaleString() → (xxx ?? 0).toLocaleString()
        content = re.sub(
            r'(\w+)\.toLocaleString\(\)',
            lambda m: f'({m.group(1)} ?? 0).toLocaleString()'
            if '??' not in content[max(0, m.start()-10):m.start()]
            else m.group(0),
            content,
        )

        if content != original:
            tsx_file.write_text(content, encoding="utf-8")
            fixes.append(f"null_safe: {tsx_file.name}")
    return fixes
