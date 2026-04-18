"""
engine/workspace/e2e_validator.py
배포 후 런타임 E2E 검증 — HTTP 요청 기반 (Playwright 불필요).

각 페이지에 대해:
  1. HTTP 200 응답 확인
  2. 응답 본문 최소 크기 (빈 페이지/에러 페이지 감지)
  3. 에러 마커 부재 (Error, 500, hydration error)
  4. HTML 구조 존재 (<main>, <div>, content)
  5. Next.js 번들 참조 (_next/static)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import asyncio
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.workspace.e2e_validator")

# 에러 마커 패턴
_ERROR_PATTERNS = [
    re.compile(r'<h1[^>]*>\s*500\s*</h1>', re.IGNORECASE),
    re.compile(r'Internal Server Error', re.IGNORECASE),
    re.compile(r'Unhandled Runtime Error', re.IGNORECASE),
    re.compile(r'Hydration failed', re.IGNORECASE),
    re.compile(r'There was an error while hydrating', re.IGNORECASE),
    re.compile(r'Text content does not match server-rendered HTML', re.IGNORECASE),
    re.compile(r'Application error: a (?:client|server)-side exception', re.IGNORECASE),
    re.compile(r'Module not found', re.IGNORECASE),
    re.compile(r'Cannot find module', re.IGNORECASE),
    re.compile(r'SyntaxError:', re.IGNORECASE),
    re.compile(r'ReferenceError:', re.IGNORECASE),
    re.compile(r'TypeError:', re.IGNORECASE),
]

# 최소 응답 크기 (bytes) — 빈 에러 페이지 감지
_MIN_BODY_SIZE = 512


def run_e2e_validation(workspace_path: Path, ports: dict) -> dict:
    """배포된 앱의 런타임 E2E 검증.

    Args:
        workspace_path: 워크스페이스 루트
        ports: {"frontend": int, "backend": int}

    Returns:
        {
            "passed": bool,
            "total_pages": int,
            "passed_pages": int,
            "failed_pages": int,
            "results": [...],
            "summary": str,
        }
    """
    fe_port = ports.get("frontend")
    if not fe_port:
        return _empty_result("프론트엔드 포트 미지정")

    # 1) 검증 대상 페이지 경로 추출
    page_routes = _discover_page_routes(workspace_path)
    if not page_routes:
        page_routes = ["/"]  # 최소 루트 페이지

    base_url = f"http://localhost:{fe_port}"
    results: list[dict] = []
    passed_count = 0

    for route in page_routes:
        url = f"{base_url}{route}"
        page_result = _validate_page(url, route)
        results.append(page_result)
        if page_result["passed"]:
            passed_count += 1

    # 2) 백엔드 API 헬스체크 (있으면)
    be_port = ports.get("backend")
    if be_port:
        api_health = _validate_api_health(be_port)
        results.append(api_health)
        if api_health["passed"]:
            passed_count += 1

    total = len(results)
    all_passed = passed_count == total
    failed_count = total - passed_count

    summary = f"E2E 검증: {passed_count}/{total} 통과"
    if not all_passed:
        failed_routes = [r["route"] for r in results if not r["passed"]]
        summary += f" | 실패: {', '.join(failed_routes[:5])}"

    logger.info(
        "e2e_validation_done passed=%d/%d all_ok=%s",
        passed_count, total, all_passed,
    )

    return {
        "passed": all_passed,
        "total_pages": total,
        "passed_pages": passed_count,
        "failed_pages": failed_count,
        "results": results,
        "summary": summary,
    }


def _discover_page_routes(workspace_path: Path) -> list[str]:
    """프론트엔드 페이지 파일로부터 라우트 경로 추출."""
    routes: list[str] = ["/"]
    fe_dir = workspace_path / "frontend"

    if not fe_dir.is_dir():
        return routes

    # Next.js App Router: src/app/(main)/*/page.tsx
    app_dir = fe_dir / "src" / "app"
    if not app_dir.is_dir():
        return routes

    for page_file in app_dir.rglob("page.tsx"):
        rel = page_file.relative_to(app_dir)
        parts = list(rel.parts)[:-1]  # page.tsx 제외

        # (main), (auth) 등 그룹 라우트 제거
        route_parts: list[str] = []
        for part in parts:
            if part.startswith("(") and part.endswith(")"):
                continue  # 그룹 라우트는 URL에 포함 안 됨
            if part.startswith("[") and part.endswith("]"):
                continue  # 동적 라우트는 스킵
            route_parts.append(part)

        route = "/" + "/".join(route_parts) if route_parts else "/"
        if route not in routes:
            routes.append(route)

    # page.jsx도 스캔
    for page_file in app_dir.rglob("page.jsx"):
        rel = page_file.relative_to(app_dir)
        parts = list(rel.parts)[:-1]
        route_parts = []
        for part in parts:
            if part.startswith("(") and part.endswith(")"):
                continue
            if part.startswith("[") and part.endswith("]"):
                continue
            route_parts.append(part)
        route = "/" + "/".join(route_parts) if route_parts else "/"
        if route not in routes:
            routes.append(route)

    return routes


async def _validate_page(url: str, route: str) -> dict:
    """단일 페이지 HTTP 검증."""
    checks: list[dict] = []
    errors: list[str] = []

    # curl로 페이지 요청
    try:
        proc = await asyncio.to_thread(subprocess.run, 
            [
                "curl", "-s",
                "-o", "-",
                "-w", "\n__HTTP_CODE__%{http_code}",
                "--max-time", "10",
                "--connect-timeout", "5",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout or ""
    except subprocess.TimeoutExpired:
        return {
            "route": route,
            "url": url,
            "passed": False,
            "checks": [],
            "errors": ["요청 타임아웃 (15초)"],
        }
    except Exception as e:
        return {
            "route": route,
            "url": url,
            "passed": False,
            "checks": [],
            "errors": [f"curl 실행 실패: {e}"],
        }

    # HTTP 상태 코드 추출
    http_code = 0
    body = output
    if "__HTTP_CODE__" in output:
        parts = output.rsplit("__HTTP_CODE__", 1)
        body = parts[0]
        try:
            http_code = int(parts[1].strip())
        except (ValueError, IndexError):
            http_code = 0

    # Check 1: HTTP 200
    c1_pass = http_code == 200
    checks.append({"name": "http_status", "pass": c1_pass, "code": http_code})
    if not c1_pass:
        errors.append(f"HTTP {http_code} (expected 200)")

    # Check 2: 최소 응답 크기
    body_size = len(body.encode("utf-8", errors="replace"))
    c2_pass = body_size >= _MIN_BODY_SIZE
    checks.append({"name": "body_size", "pass": c2_pass, "size": body_size})
    if not c2_pass:
        errors.append(f"응답 본문 {body_size}B (최소 {_MIN_BODY_SIZE}B)")

    # Check 3: 에러 마커 부재
    found_errors: list[str] = []
    for pattern in _ERROR_PATTERNS:
        m = pattern.search(body)
        if m:
            found_errors.append(m.group(0)[:80])
    c3_pass = len(found_errors) == 0
    checks.append({"name": "no_error_markers", "pass": c3_pass, "found": found_errors[:3]})
    if not c3_pass:
        errors.append(f"에러 마커 감지: {', '.join(found_errors[:3])}")

    # Check 4: HTML 구조 확인
    has_html_structure = (
        ("<div" in body or "<main" in body or "<section" in body)
        and ("</div>" in body or "</main>" in body or "</section>" in body)
    )
    checks.append({"name": "html_structure", "pass": has_html_structure})
    if not has_html_structure:
        errors.append("HTML 구조 미확인 (<div>/<main> 태그 없음)")

    # Check 5: Hydration 에러 마커 (Next.js 전용)
    hydration_ok = "hydration" not in body.lower() or "Hydration failed" not in body
    checks.append({"name": "no_hydration_error", "pass": hydration_ok})

    # Check 6: Next.js 번들 참조
    has_bundle = "_next/static" in body or "_next/data" in body or "__next" in body
    checks.append({"name": "js_bundle_ref", "pass": has_bundle})

    all_passed = all(c["pass"] for c in checks)
    return {
        "route": route,
        "url": url,
        "passed": all_passed,
        "checks": checks,
        "errors": errors,
    }


async def _validate_api_health(be_port: int) -> dict:
    """백엔드 API 헬스체크."""
    checks: list[dict] = []
    errors: list[str] = []

    # 공통 헬스 엔드포인트 시도
    health_paths = ["/health", "/api/health", "/api/v1/health", "/"]
    responsive = False
    used_path = ""

    for path in health_paths:
        url = f"http://localhost:{be_port}{path}"
        try:
            proc = await asyncio.to_thread(subprocess.run, 
                [
                    "curl", "-s",
                    "-o", "/dev/null",
                    "-w", "%{http_code}",
                    "--max-time", "5",
                    "--connect-timeout", "3",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            code = int(proc.stdout.strip()) if proc.stdout.strip().isdigit() else 0
            if code in (200, 201, 204, 301, 302, 404):
                # 404도 서버 자체는 살아있음
                responsive = True
                used_path = path
                break
        except Exception:
            continue

    checks.append({
        "name": "api_responsive",
        "pass": responsive,
        "port": be_port,
        "path": used_path,
    })
    if not responsive:
        errors.append(f"백엔드 API (port {be_port}) 응답 없음")

    return {
        "route": f"API:{be_port}",
        "url": f"http://localhost:{be_port}",
        "passed": responsive,
        "checks": checks,
        "errors": errors,
    }


def _empty_result(reason: str) -> dict:
    return {
        "passed": False,
        "total_pages": 0,
        "passed_pages": 0,
        "failed_pages": 0,
        "results": [],
        "summary": reason,
    }
