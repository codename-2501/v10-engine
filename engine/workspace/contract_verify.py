"""
engine/workspace/contract_verify.py
API 계약 검증 + 시각 스모크 테스트.

프론트/백 데이터 계약 불일치 감지 및 페이지별 스크린샷 검증.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import asyncio
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.workspace.contract_verify")


# ============================================================
# 프론트/백 데이터 계약 검증
# ============================================================

def verify_api_contracts(
    workspace_path: Path,
    stack: dict,
    be_port: int,
) -> list[dict]:
    """프론트엔드 타입 정의 vs 백엔드 실제 API 응답 비교."""
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"
    mismatches = []

    # 1. 프론트엔드 타입 파일에서 인터페이스 추출
    fe_types = _extract_frontend_types(fe)

    # 2. 백엔드 API 응답에서 필드명 추출
    token = _get_test_token(be_port)
    if not token:
        return [{"type": "auth_failed", "message": "테스트 로그인 실패"}]

    # 3. 주요 엔드포인트 호출 → 응답 필드와 프론트 타입 비교
    api_base = f"http://localhost:{be_port}/api/v1"
    checks = [
        ("/pets", "Pet"),
        ("/posts", "CommunityPost"),
        ("/products", "Product"),
    ]

    for path, type_name in checks:
        if type_name not in fe_types:
            continue

        try:
            import urllib.request
            req = urllib.request.Request(
                f"{api_base}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
        except Exception:
            continue

        # 응답에서 첫 번째 아이템의 필드 추출
        data = body.get("data", body)
        items = data.get("items", data) if isinstance(data, dict) else data
        if isinstance(items, list) and items:
            api_fields = set(items[0].keys())
        elif isinstance(items, dict):
            api_fields = set(items.keys())
        else:
            continue

        fe_fields = fe_types[type_name]

        # 프론트에서 기대하는데 백에 없는 필드
        missing_in_api = fe_fields - api_fields - {"id", "createdAt", "updatedAt"}
        # 백에 있는데 프론트 타입에 없는 필드 (경고 수준)
        extra_in_api = api_fields - fe_fields - {"id", "createdAt", "updatedAt"}

        if missing_in_api:
            mismatches.append({
                "type": "missing_field",
                "endpoint": path,
                "fe_type": type_name,
                "fields": list(missing_in_api),
                "severity": "error",
                "message": f"프론트엔드 {type_name}이 기대하는 필드가 API에 없음: {missing_in_api}",
            })

        if extra_in_api and len(extra_in_api) > 3:
            mismatches.append({
                "type": "extra_field",
                "endpoint": path,
                "fe_type": type_name,
                "fields": list(extra_in_api),
                "severity": "warning",
                "message": f"API에 있지만 프론트엔드 {type_name}에 정의 안 된 필드: {extra_in_api}",
            })

    logger.info("contract_verify_done mismatches=%d", len(mismatches))
    return mismatches


def _extract_frontend_types(fe: Path) -> dict[str, set[str]]:
    """프론트엔드 types.ts에서 인터페이스 → 필드셋 추출."""
    types = {}

    for ts_file in fe.rglob("types.ts"):
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # export interface Pet { ... }
        for m in re.finditer(
            r"(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+\w+)?\s*\{([^}]+)\}",
            content,
        ):
            name = m.group(1)
            body = m.group(2)
            fields = set()
            for line in body.strip().split("\n"):
                field_match = re.match(r"\s*(\w+)\s*[?:]", line.strip())
                if field_match:
                    fields.add(field_match.group(1))
            if fields:
                types[name] = fields

    return types


def _get_test_token(be_port: int) -> str | None:
    """테스트 계정으로 토큰 획득."""
    import urllib.request
    for email in ["test@test.com", "admin@test.com", "test@test.kr", "admin@example.com", "user@test.com"]:
        try:
            data = json.dumps({"email": email, "password": "test1234"}).encode()
            req = urllib.request.Request(
                f"http://localhost:{be_port}/api/v1/auth/login",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                return body.get("data", {}).get("accessToken")
        except Exception:
            continue
    return None


# ============================================================
# 시각 검증 — 페이지별 스크린샷 + 빈 화면/에러 감지
# ============================================================

async def visual_smoke_test(
    workspace_path: Path,
    fe_port: int,
    be_port: int,
    routes: list[str] | None = None,
) -> dict:
    """모든 페이지 스크린샷 → 빈 화면/에러 페이지 감지."""
    fe = workspace_path / "frontend"
    screenshots_dir = workspace_path / "test-screenshots"
    screenshots_dir.mkdir(exist_ok=True)

    if not routes:
        # auto-detect
        from engine.workspace.test_generator import _detect_frontend_routes
        detected = _detect_frontend_routes(fe)
        routes = [r["path"] for r in detected if not r["is_dynamic"] and not r["is_public"]]

    # auth store 이름 동적 감지
    auth_store_name = _detect_auth_store_name(fe)

    # Playwright 스크린샷 스크립트 생성
    script = _build_screenshot_script(fe_port, be_port, routes, screenshots_dir, auth_store_name)
    script_path = fe / "e2e" / "_screenshot.ts"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")

    # 실행
    result = await asyncio.to_thread(subprocess.run, 
        ["npx", "playwright", "test", "e2e/_screenshot.ts", "--reporter=list"],
        cwd=fe, capture_output=True, text=True, timeout=120,
    )

    passed = len(re.findall(r"✓", result.stdout + result.stderr))
    failed = len(re.findall(r"✘", result.stdout + result.stderr))

    logger.info(
        "visual_smoke_done routes=%d passed=%d failed=%d screenshots=%s",
        len(routes), passed, failed, screenshots_dir,
    )

    return {
        "total": len(routes),
        "passed": passed,
        "failed": failed,
        "screenshots_dir": str(screenshots_dir),
    }


def _detect_auth_store_name(fe: Path) -> str:
    """프론트엔드 zustand store에서 persist name을 동적 감지."""
    for store_file in fe.rglob("*Store*"):
        if store_file.suffix not in (".ts", ".tsx") or "node_modules" in str(store_file):
            continue
        try:
            content = store_file.read_text(encoding="utf-8")
            if "persist" in content and "token" in content:
                m = re.search(r"name:\s*['\"]([^'\"]+)['\"]", content)
                if m:
                    return m.group(1)
        except Exception:
            continue
    # 미들웨어에서 쿠키명 감지
    for mw_file in fe.rglob("middleware.*"):
        if "node_modules" in str(mw_file):
            continue
        try:
            content = mw_file.read_text(encoding="utf-8")
            m = re.search(r"cookies?\.get\(['\"]([^'\"]+)['\"]", content)
            if m:
                return m.group(1)
        except Exception:
            continue
    return "auth-storage"  # 범용 폴백


def _build_screenshot_script(
    fe_port: int,
    be_port: int,
    routes: list[str],
    screenshots_dir: Path,
    auth_store_name: str = "auth-storage",
) -> str:
    """페이지별 스크린샷 + 빈 화면 검증 Playwright 스크립트."""
    lines = [
        "import { test, expect } from '@playwright/test';",
        "",
        f"const API = 'http://localhost:{be_port}/api/v1';",
        "const CREDS = { email: 'test@test.com', password: 'test1234' };",
        "",
        "async function login(page: any) {",
        "  const res = await fetch(`${API}/auth/login`, {",
        "    method: 'POST',",
        "    headers: { 'Content-Type': 'application/json' },",
        "    body: JSON.stringify(CREDS),",
        "  });",
        "  const body = await res.json();",
        "  const data = body.data ?? body;",
        "  const token = data.accessToken ?? data.token;",
        "  await page.goto('/login');",
        "  await page.evaluate((t: string) => {",
        f"    localStorage.setItem('{auth_store_name}', JSON.stringify({{ state: {{ token: t }}, version: 0 }}));",
        f"    document.cookie = `{auth_store_name}=${{encodeURIComponent(JSON.stringify({{ state: {{ token: t }} }}))}};path=/;max-age=3600`;",
        "  }, token);",
        "}",
        "",
    ]

    for route in routes:
        safe_name = route.replace("/", "_").strip("_") or "home"
        lines.append(f"test('스크린샷 {route}', async ({{ page }}) => {{")
        lines.append(f"  await login(page);")
        lines.append(f"  await page.goto('{route}');")
        lines.append(f"  await page.waitForTimeout(1500);")
        lines.append(f"  // 빈 화면 체크: body 높이가 100px 이상이어야 함")
        lines.append(f"  const height = await page.evaluate(() => document.body.scrollHeight);")
        lines.append(f"  expect(height).toBeGreaterThan(100);")
        lines.append(f"  // 에러 텍스트 부재")
        lines.append(f"  const text = await page.locator('body').textContent();")
        lines.append(f"  expect(text).not.toContain('Internal Server Error');")
        lines.append(f"  expect(text).not.toContain('Application error');")
        lines.append(f"  // 스크린샷 저장")
        lines.append(f"  await page.screenshot({{ path: '{screenshots_dir}/{safe_name}.png', fullPage: true }});")
        lines.append(f"}});")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# 통합 실행
# ============================================================

def run_full_verification(
    workspace_path: Path,
    stack: dict,
    fe_port: int,
    be_port: int,
) -> dict:
    """전체 검증 파이프라인 실행."""
    from engine.workspace.verify_and_fix import build_fix_loop

    report: dict[str, Any] = {}

    # 1. 빌드-수정 루프
    logger.info("verify_step1_build_fix_loop")
    report["build"] = build_fix_loop(workspace_path, stack)

    # 2. 데이터 계약 검증
    logger.info("verify_step2_contract_check")
    report["contracts"] = verify_api_contracts(workspace_path, stack, be_port)

    # 3. 시각 검증
    logger.info("verify_step3_visual_smoke")
    report["visual"] = visual_smoke_test(workspace_path, fe_port, be_port)

    # 결과 저장
    report_path = workspace_path / "verify-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    logger.info(
        "verify_complete build=%s contracts=%d visual_passed=%s/%s report=%s",
        report["build"].get("backend", {}).get("success", "N/A"),
        len(report["contracts"]),
        report["visual"].get("passed", 0),
        report["visual"].get("total", 0),
        report_path,
    )

    return report
