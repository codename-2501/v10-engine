"""
engine/workspace/api_connector.py
프론트엔드 mock 데이터 → 실제 백엔드 API 자동 연결.

auto_deploy 배포 시 프론트엔드가 mock 데이터를 사용하는 경우,
백엔드 API 엔드포인트로 자동 교체하여 실 데이터를 사용하도록 함.
mock 데이터는 fallback으로 보존.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.workspace.api_connector")

# mock 데이터 배열 패턴: const NAME = [...]; 또는 const NAME: Type[] = [...];
_MOCK_ARRAY_PATTERN = re.compile(
    r'^(\s*)(export\s+)?const\s+(\w+)(?:\s*:\s*\w+(?:\[\])?)?\s*=\s*\[',
    re.MULTILINE,
)

# 리소스 이름 → API 경로 매핑 힌트
_RESOURCE_HINTS: dict[str, str] = {
    "user": "users",
    "caregiver": "caregivers",
    "patient": "patients",
    "resident": "residents",
    "schedule": "schedules",
    "appointment": "appointments",
    "notification": "notifications",
    "report": "reports",
    "dashboard": "dashboard",
    "staff": "staff",
    "employee": "employees",
    "service": "services",
    "facility": "facilities",
    "room": "rooms",
    "medication": "medications",
    "activity": "activities",
    "message": "messages",
    "document": "documents",
    "payment": "payments",
    "invoice": "invoices",
    "order": "orders",
    "product": "products",
    "category": "categories",
    "review": "reviews",
    "comment": "comments",
    "post": "posts",
    "event": "events",
    "task": "tasks",
    "project": "projects",
    "member": "members",
    "team": "teams",
}

# mock 데이터로 보이지 않는 변수명 제외 패턴
_SKIP_NAMES = {
    "columns", "headers", "options", "tabs", "menuItems", "navItems",
    "links", "breadcrumbs", "steps", "fields", "validations",
    "defaultValues", "initialState", "config", "settings",
    "COLORS", "SIZES", "BREAKPOINTS", "ROUTES",
}


def connect_frontend_to_backend(workspace_path: Path, ports: dict) -> dict:
    """프론트엔드 mock 데이터를 백엔드 API fetch 호출로 교체.

    Args:
        workspace_path: 워크스페이스 루트
        ports: {"frontend": int, "backend": int}

    Returns:
        {"connected": int, "pages": [...], "env_created": bool}
    """
    fe_dir = workspace_path / "frontend"
    be_port = ports.get("backend", 4200)
    results: list[dict] = []

    if not fe_dir.is_dir():
        logger.info("api_connector_skip: no frontend dir")
        return {"connected": 0, "pages": [], "env_created": False}

    # 1) 백엔드 API 엔드포인트 탐색
    api_endpoints = _discover_api_endpoints(workspace_path)
    logger.info("api_connector_endpoints_found count=%d", len(api_endpoints))

    # 2) 프론트엔드 페이지 파일 스캔
    page_files = list(fe_dir.rglob("page.tsx")) + list(fe_dir.rglob("page.jsx"))
    connected_count = 0

    for page_file in page_files:
        try:
            original = page_file.read_text(encoding="utf-8")
        except Exception:
            continue

        modified, replacements = _replace_mock_with_fetch(
            original, api_endpoints, be_port,
        )

        if replacements:
            # useState/useEffect import 보장
            modified = _ensure_react_imports(modified)
            page_file.write_text(modified, encoding="utf-8")
            connected_count += len(replacements)
            results.append({
                "file": str(page_file.relative_to(workspace_path)),
                "replacements": replacements,
            })
            logger.info(
                "api_connector_replaced file=%s count=%d",
                page_file.relative_to(workspace_path), len(replacements),
            )

    # 3) .env.local 생성
    env_created = _write_env_local(fe_dir, be_port)

    logger.info("api_connector_done connected=%d pages=%d", connected_count, len(results))
    return {"connected": connected_count, "pages": results, "env_created": env_created}


def _discover_api_endpoints(workspace_path: Path) -> list[str]:
    """백엔드에서 사용 가능한 API 엔드포인트 목록을 추출."""
    endpoints: list[str] = []
    be_dir = workspace_path / "backend"

    if not be_dir.is_dir():
        return endpoints

    # Express/Fastify 라우트 파일 스캔
    route_pattern = re.compile(
        r'''(?:router|app)\.(get|post|put|patch|delete)\s*\(\s*['"]([^'"]+)['"]''',
        re.IGNORECASE,
    )
    for ts_file in list(be_dir.rglob("*.ts")) + list(be_dir.rglob("*.js")):
        if "node_modules" in str(ts_file) or ".venv" in str(ts_file):
            continue
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in route_pattern.finditer(content):
            endpoint = m.group(2)
            if endpoint and endpoint.startswith("/"):
                endpoints.append(endpoint)

    # FastAPI/Flask 라우트 파일 스캔
    py_route_pattern = re.compile(
        r'''@(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*['"]([^'"]+)['"]''',
        re.IGNORECASE,
    )
    for py_file in be_dir.rglob("*.py"):
        if ".venv" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in py_route_pattern.finditer(content):
            endpoint = m.group(2)
            if endpoint and endpoint.startswith("/"):
                endpoints.append(endpoint)

    # swagger.json이 있으면 파싱
    swagger_file = be_dir / "swagger.json"
    if swagger_file.is_file():
        try:
            spec = json.loads(swagger_file.read_text(encoding="utf-8"))
            paths = spec.get("paths", {})
            endpoints.extend(paths.keys())
        except Exception:
            pass

    return sorted(set(endpoints))


def _match_endpoint(var_name: str, api_endpoints: list[str]) -> str | None:
    """변수명으로부터 가장 적합한 API 엔드포인트를 매칭."""
    # 변수명을 소문자로 정규화하고 단어 분리
    normalized = re.sub(r'([A-Z])', r'_\1', var_name).lower().strip("_")
    words = set(re.split(r'[_\s]+', normalized))

    # 일반적인 접두사/접미사 제거
    words -= {"all", "mock", "data", "rows", "list", "items", "sample", "fake", "dummy", "initial", "default"}

    if not words:
        return None

    # 1) 직접 엔드포인트 매칭
    best_match: str | None = None
    best_score = 0

    for endpoint in api_endpoints:
        ep_lower = endpoint.lower()
        ep_words = set(re.split(r'[/_\-]+', ep_lower)) - {"api", "v1", "v2", ""}
        overlap = len(words & ep_words)
        if overlap > best_score:
            best_score = overlap
            best_match = endpoint

    if best_match and best_score > 0:
        return best_match

    # 2) 리소스 힌트 테이블 매칭
    for word in words:
        for hint_key, hint_path in _RESOURCE_HINTS.items():
            if hint_key in word or word in hint_key:
                # API 엔드포인트 중에 이 리소스가 있는지
                for ep in api_endpoints:
                    if hint_path in ep.lower():
                        return ep
                # 없으면 추측 경로 생성
                return f"/api/v1/{hint_path}"

    return None


def _find_array_end(text: str, start_bracket: int) -> int:
    """배열 리터럴의 끝 위치(];)를 찾음. 중첩 괄호 처리."""
    depth = 0
    i = start_bracket
    in_string = False
    string_char = ""
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == string_char:
                in_string = False
        else:
            if ch in ('"', "'", "`"):
                in_string = True
                string_char = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    # 세미콜론 포함
                    end = i + 1
                    if end < len(text) and text[end] == ";":
                        end += 1
                    return end
        i += 1
    return -1


def _replace_mock_with_fetch(
    source: str,
    api_endpoints: list[str],
    be_port: int,
) -> tuple[str, list[dict]]:
    """소스 코드에서 mock 배열을 찾아 API fetch + fallback 패턴으로 교체."""
    replacements: list[dict] = []
    result = source

    # mock 배열 찾기 (뒤에서부터 처리하여 offset 유지)
    matches = list(_MOCK_ARRAY_PATTERN.finditer(source))

    for match in reversed(matches):
        indent = match.group(1)
        export_kw = match.group(2) or ""
        var_name = match.group(3)

        # 스킵 대상 확인
        if var_name in _SKIP_NAMES:
            continue
        if var_name.isupper() and len(var_name) > 2:
            # 상수 (COLORS, CONFIG 등) 스킵
            if not any(h in var_name.lower() for h in ("data", "mock", "row", "item", "user", "list")):
                continue
        # 배열 원소가 2개 미만이면 mock이 아닐 가능성
        bracket_pos = source.find("[", match.start())
        if bracket_pos < 0:
            continue
        array_end = _find_array_end(source, bracket_pos)
        if array_end < 0:
            continue

        array_text = source[bracket_pos:array_end]
        # 객체 원소({...})가 최소 1개 있어야 mock 데이터로 간주
        if array_text.count("{") < 1:
            continue

        # API 엔드포인트 매칭
        endpoint = _match_endpoint(var_name, api_endpoints)
        if not endpoint:
            continue

        # 교체 코드 생성
        fallback_name = f"FALLBACK_{var_name.upper()}"
        original_block = source[match.start():array_end]

        # fallback 상수 + useState + useEffect
        new_block = (
            f"{indent}const {fallback_name} = {array_text.rstrip(';')}\n"
            f"{indent}const [{var_name}, set{var_name[0].upper()}{var_name[1:]}] = useState({fallback_name});\n"
            f"{indent}useEffect(() => {{\n"
            f"{indent}  fetch(`${{process.env.NEXT_PUBLIC_API_URL || 'http://localhost:{be_port}'}}{endpoint}`)\n"
            f"{indent}    .then(r => r.ok ? r.json() : null)\n"
            f"{indent}    .then(data => {{ if (data?.items) set{var_name[0].upper()}{var_name[1:]}(data.items); else if (Array.isArray(data)) set{var_name[0].upper()}{var_name[1:]}(data); }})\n"
            f"{indent}    .catch(() => {{}});  // fallback to mock\n"
            f"{indent}}}, []);"
        )

        result = result[:match.start()] + new_block + result[array_end:]
        replacements.append({
            "variable": var_name,
            "endpoint": endpoint,
            "fallback": fallback_name,
        })

    return result, replacements


def _ensure_react_imports(source: str) -> str:
    """useState, useEffect가 import되어 있는지 확인하고 없으면 추가."""
    needs_use_state = "useState" not in source.split("\n")[0:20].__repr__() and "useState(" in source
    needs_use_effect = "useEffect" not in source.split("\n")[0:20].__repr__() and "useEffect(" in source

    if not needs_use_state and not needs_use_effect:
        return source

    hooks_needed: list[str] = []
    if needs_use_state:
        hooks_needed.append("useState")
    if needs_use_effect:
        hooks_needed.append("useEffect")

    # 기존 react import 찾기
    react_import_pattern = re.compile(
        r'''(import\s+\{[^}]*\}\s+from\s+['"]react['"];?)''',
        re.MULTILINE,
    )
    m = react_import_pattern.search(source)
    if m:
        existing = m.group(1)
        for hook in hooks_needed:
            if hook not in existing:
                # import { X } from 'react'; 에 hook 추가
                source = source.replace(
                    existing,
                    existing.replace("}", f", {hook} }}").replace("} }", "}"),
                )
                # re-fetch the updated import
                m2 = react_import_pattern.search(source)
                if m2:
                    existing = m2.group(1)
    else:
        # react import 자체가 없으면 파일 맨 위에 추가
        hooks_str = ", ".join(hooks_needed)
        source = f"import {{ {hooks_str} }} from 'react';\n" + source

    return source


def _write_env_local(fe_dir: Path, be_port: int) -> bool:
    """프론트엔드 .env.local 파일 생성/업데이트."""
    env_file = fe_dir / ".env.local"
    api_url_line = f"NEXT_PUBLIC_API_URL=http://localhost:{be_port}"

    if env_file.is_file():
        content = env_file.read_text(encoding="utf-8")
        if "NEXT_PUBLIC_API_URL" in content:
            # 이미 설정됨 → 포트만 업데이트
            content = re.sub(
                r'NEXT_PUBLIC_API_URL=.*',
                api_url_line,
                content,
            )
            env_file.write_text(content, encoding="utf-8")
        else:
            with env_file.open("a", encoding="utf-8") as f:
                f.write(f"\n{api_url_line}\n")
    else:
        env_file.write_text(f"{api_url_line}\n", encoding="utf-8")

    return True
