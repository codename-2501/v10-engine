from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SLUG_PREFIX_LABELS = {
    "admin": "관리자",
    "bo": "백오피스",
    "caregiver": "요양보호사",
    "carer": "요양보호사",
    "elder": "어르신",
    "family": "보호자",
    "shop": "쇼핑",
    "community": "커뮤니티",
    "health": "건강관리",
    "hospital": "병원",
    "sitter": "돌봄",
    "pet": "반려동물",
    "booking": "예약",
    "dm": "메시지",
    "rating": "평가",
}


def _py_to_js_literal(value: Any) -> str:
    """Python 값 → JavaScript 리터럴 문자열. True→true, False→false, None→null 등."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # 쌍따옴표 + 이스케이프 처리
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value))


def _sql_type_to_ts(sql_type: str) -> str:
    """SQL 타입 → TypeScript 타입 매핑."""
    t = sql_type.upper()
    if any(k in t for k in ("INT", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")):
        return "number"
    if any(k in t for k in ("BOOL",)):
        return "boolean"
    if any(k in t for k in ("JSON", "JSONB")):
        return "Record<string, any>"
    if any(k in t for k in ("DATE", "TIME", "TIMESTAMP")):
        return "string"  # ISO 문자열
    if any(k in t for k in ("BLOB", "BYTEA")):
        return "Uint8Array"
    return "string"


def _slug_to_resource(slug: str) -> str:
    """페이지 slug에서 API 리소스명 추정.

    admin-user-list → users, elder-health-detail → health, shop-product → products
    """
    parts = slug.split("-")
    # 접두사(admin, elder 등) 제거
    if parts and parts[0] in _SLUG_PREFIX_LABELS:
        parts = parts[1:]
    # 접미사(list, detail, form, create, edit) 제거
    suffixes = {"list", "detail", "form", "create", "edit", "new", "index", "page"}
    while parts and parts[-1] in suffixes:
        parts.pop()
    resource = "-".join(parts) if parts else slug
    return resource


def _match_endpoints_for_page(
    slug: str,
    endpoints: list[dict],
    page_type: str,
) -> dict[str, dict]:
    """페이지 slug와 타입에 맞는 API 엔드포인트 매칭.

    Returns: {"list": {endpoint}, "detail": {endpoint}, "create": {endpoint}, ...}
    """
    resource = _slug_to_resource(slug)
    matched: dict[str, dict] = {}

    for ep in endpoints:
        path_lower = ep["path"].lower()
        # 리소스명이 경로에 포함되는지
        if resource.replace("-", "") not in path_lower.replace("-", "").replace("_", "").replace("/", ""):
            continue

        method = ep["method"].upper()
        has_id = "{" in ep["path"] or ":id" in ep["path"]

        if method == "GET" and not has_id:
            matched.setdefault("list", ep)
        elif method == "GET" and has_id:
            matched.setdefault("detail", ep)
        elif method == "POST":
            matched.setdefault("create", ep)
        elif method in ("PUT", "PATCH"):
            matched.setdefault("update", ep)
        elif method == "DELETE":
            matched.setdefault("delete", ep)

    return matched


def _detect_page_type(slug: str, placements: list) -> str:
    """페이지 타입 감지: list, detail, form, dashboard."""
    slug_lower = slug.lower()
    if any(k in slug_lower for k in ("list", "목록", "index")):
        return "list"
    if any(k in slug_lower for k in ("detail", "상세", "view")):
        return "detail"
    if any(k in slug_lower for k in ("form", "create", "edit", "new", "등록", "수정")):
        return "form"
    if any(k in slug_lower for k in ("dashboard", "대시보드", "home", "main")):
        return "dashboard"

    # placement 기반 추론
    comp_names = [p.component_name for p in placements]
    if any("table" in c or "list" in c for c in comp_names):
        return "list"
    if any("form" in c or "input" in c for c in comp_names):
        return "form"
    if any("chart" in c or "stat" in c for c in comp_names):
        return "dashboard"

    return "detail"


def _safe_optional_chain(source_path: str) -> str:
    """(6) source_path → optional chaining 적용.

    "items[0].user.name" → "data?.items?.[0]?.user?.name"
    "stats" → "data?.stats"
    """
    if not source_path:
        return "data"
    # data 접두어가 이미 있으면 제거 후 재적용
    sp = source_path
    if sp.startswith("data?."):
        sp = sp[6:]
    elif sp.startswith("data."):
        sp = sp[5:]
    parts = sp.replace("[", ".[").split(".")
    result = "data"
    for part in parts:
        if not part:
            continue
        if part.startswith("["):
            result += f"?.{part}"
        else:
            result += f"?.{part}"
    return result


def _py_condition_to_jsx(condition: str) -> str:
    """(7) Python 조건식 → JSX 조건 변환.

    "if data.items" → "data?.items?.length"
    "unless empty" → "data"
    "if True" → "true"
    "if not data.active" → "!data?.active"
    """
    c = condition.strip()
    # 접두어 제거
    if c.startswith("if "):
        c = c[3:].strip()
    elif c.startswith("unless "):
        c = "!" + c[7:].strip()

    # Python bool → JS
    c = c.replace("True", "true").replace("False", "false").replace("None", "null")

    # Python not → JS !
    c = c.replace("not ", "!")

    # Python and/or → JS &&/||
    c = c.replace(" and ", " && ").replace(" or ", " || ")

    # data.xxx → data?.xxx (optional chaining)
    if "data." in c and "?." not in c:
        c = c.replace("data.", "data?.")

    return c


def _py_data_path_to_js(repeat: str) -> str:
    """(7) Python 데이터 경로 → JS optional chaining.

    "data.items" → "data?.items"
    "data['items']" → "data?.items"
    """
    r = repeat.strip()
    # Python dict access → dot
    import re as _re
    r = _re.sub(r"\['(\w+)'\]", r".\1", r)
    r = _re.sub(r'\["(\w+)"\]', r".\1", r)
    # optional chaining
    if "." in r and "?." not in r:
        parts = r.split(".")
        r = "?.".join(parts)
    return r


def _auto_props_for_component(component_name: str, page_type: str, resource: str) -> str:
    """컴포넌트 이름에서 자동 props 추정 (바인딩 없을 때 폴백)."""
    cn = component_name.lower()
    if "table" in cn or "list" in cn:
        return " data={data?.items ?? []} onDelete={handleDelete}"
    if "form" in cn or "input" in cn:
        return " register={register} errors={errors}"
    if "chart" in cn or "stat" in cn or "card" in cn:
        return " data={data?.stats ?? {}}"
    if "pagination" in cn or "pager" in cn:
        return " page={data?.page ?? 1} total={data?.total ?? 0} onChange={handlePageChange}"
    if "search" in cn or "filter" in cn:
        return " onSearch={handleSearch} value={searchQuery}"
    if "header" in cn or "hero" in cn:
        return f' title="{resource}"'
    if "nav" in cn or "sidebar" in cn or "breadcrumb" in cn:
        return ""
    return ""


def _sql_to_prisma_type(sql_type: str) -> str:
    """SQL 타입 → Prisma scalar 타입."""
    t = sql_type.upper()
    if "INT" in t:
        return "Int"
    if any(k in t for k in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")):
        return "Float"
    if "BOOL" in t:
        return "Boolean"
    if any(k in t for k in ("DATE", "TIME", "TIMESTAMP")):
        return "DateTime"
    # SQLite 호환: Json/Array → String
    if "JSON" in t:
        return "String"
    return "String"
