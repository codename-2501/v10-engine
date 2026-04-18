from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

from engine.skills.codegen.helpers import (
    _py_to_js_literal, _sql_type_to_ts, _slug_to_resource,
    _match_endpoints_for_page, _detect_page_type, _safe_optional_chain,
    _py_condition_to_jsx, _py_data_path_to_js, _auto_props_for_component,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template variable pattern: matches {word} or {word.word} but NOT JSON-like
# structures such as [{...}] or {"key": ...}
# ---------------------------------------------------------------------------
_TEMPLATE_VAR_RE = _re.compile(r'\{(\w+(?:\.\w+)*)\}')

# Sidebar/nav color props that should be omitted (let CSS variables handle them)
_SIDEBAR_COLOR_PROPS = frozenset({
    "bg_color", "text_color", "active_color", "hover_color",
    "active_bg_color", "active_text_color", "hover_bg_color",
    "hover_text_color", "border_color",
})


def _is_template_string(value: str) -> bool:
    """Check if a string contains {var} template patterns (not JSON structures).

    Returns True for: "전체 {notices.total_count}건", "{caregiver_stats.active}"
    Returns False for: "[{\"label\": ...}]", "{\"key\": \"value\"}"
    """
    if not isinstance(value, str):
        return False
    # Quick reject: if it looks like JSON array/object, skip
    stripped = value.strip()
    if stripped.startswith("[") or stripped.startswith('{"') or stripped.startswith("{'"):
        return False
    return bool(_TEMPLATE_VAR_RE.search(value))


def _convert_template_to_jsx(value: str) -> str:
    """Convert a template string with {var} patterns to JSX expression.

    - "{caregiver_stats.active}" (entire string is one var)
      → {data?.caregiver_stats?.active ?? ''}
    - "전체 {notices.total_count}건" (mixed text + var)
      → {`전체 ${data?.notices?.total_count ?? ''}건`}
    """
    # Check if entire string is a single template variable
    m = _re.fullmatch(r'\{(\w+(?:\.\w+)*)\}', value.strip())
    if m:
        path = m.group(1).replace('.', '?.')
        return '{data?.' + path + " ?? ''}"

    # Mixed text + template vars → JS template literal
    def _replace_var(match):
        path = match.group(1).replace('.', '?.')
        return '${data?.' + path + " ?? ''}"

    js_template = _TEMPLATE_VAR_RE.sub(_replace_var, value)
    return '{`' + js_template + '`}'


def _normalize_sidebar_href(href: str, all_slugs: list[str] | None) -> str:
    """Normalize sidebar/nav href to match actual page slug routes.

    /admin/caregivers → /admin-caregivers (if slug exists)
    /admin/dashboard  → /admin-dashboard  (if slug exists)
    """
    if not href or not href.startswith('/'):
        return href
    if not all_slugs:
        return href

    # Already a flat slug path like /admin-dashboard
    clean = href.strip('/')
    if clean in all_slugs:
        return href

    # Try converting /admin/caregivers → admin-caregivers
    normalized = clean.replace('/', '-')
    if normalized in all_slugs:
        return '/' + normalized

    # Try partial match: /admin/caregivers → find slug starting with admin-caregiver
    for slug in all_slugs:
        # Check if the slug is a dash-joined version of the path segments
        if slug == normalized:
            return '/' + slug
        # Partial prefix match for cases like /admin/clients → admin-client-list
        if slug.startswith(normalized.rsplit('-', 1)[0] + '-') and len(normalized) > 3:
            return '/' + slug

    return href


def _convert_stat_items(items: list) -> str:
    """Convert a StatCard items array with {var} template values to JS expression.

    Input: [{"label": "활동중", "value": "{caregiver_stats.active}", "icon": "..."}]
    Output: JS array expression with data references.
    """
    js_items = []
    for item in items:
        if not isinstance(item, dict):
            js_items.append(json.dumps(item, ensure_ascii=False))
            continue
        parts = []
        for k, v in item.items():
            if isinstance(v, str) and _is_template_string(v):
                # {caregiver_stats.active} → data?.caregiver_stats?.active ?? 0
                m = _re.fullmatch(r'\{(\w+(?:\.\w+)*)\}', v.strip())
                if m:
                    path = m.group(1).replace('.', '?.')
                    parts.append(f'{k}: data?.{path} ?? 0')
                else:
                    # Mixed template → template literal
                    def _repl(match):
                        p = match.group(1).replace('.', '?.')
                        return '${data?.' + p + " ?? ''}"
                    tpl = _TEMPLATE_VAR_RE.sub(_repl, v)
                    parts.append(f'{k}: `{tpl}`')
            elif isinstance(v, str):
                parts.append(f'{k}: {json.dumps(v, ensure_ascii=False)}')
            elif isinstance(v, bool):
                parts.append(f'{k}: {"true" if v else "false"}')
            elif isinstance(v, (int, float)):
                parts.append(f'{k}: {v}')
            elif v is None:
                parts.append(f'{k}: null')
            else:
                parts.append(f'{k}: {json.dumps(v, ensure_ascii=False)}')
        js_items.append('{ ' + ', '.join(parts) + ' }')
    return '[' + ', '.join(js_items) + ']'


def _fix_menu_items_hrefs(items: list, all_slugs: list[str] | None) -> list:
    """Fix href values in menu_items list to match actual page slugs (Bug 6)."""
    if not all_slugs:
        return items
    fixed = []
    for item in items:
        if not isinstance(item, dict):
            fixed.append(item)
            continue
        new_item = dict(item)
        if 'href' in new_item and isinstance(new_item['href'], str):
            new_item['href'] = _normalize_sidebar_href(new_item['href'], all_slugs)
        # Recursively fix nested children/sub_items
        for child_key in ('children', 'sub_items', 'items', 'subMenu'):
            if child_key in new_item and isinstance(new_item[child_key], list):
                new_item[child_key] = _fix_menu_items_hrefs(new_item[child_key], all_slugs)
        fixed.append(new_item)
    return fixed


def _map_source_path_to_data(source_path: str, slot_name: str, component_name: str, page_type: str) -> str:
    """Map recipe source_path to actual data structure references (Bug 2).

    Recipe bindings use idealized paths like 'caregivers.list', but fetchData
    stores data in a flat structure: {items: [...], total, page, pageSize}.
    """
    sp = source_path
    cn = component_name.lower()

    # .list or .items suffix → data?.items ?? []
    if '.list' in sp or '.items' in sp:
        return 'data?.items ?? []'

    # pagination paths → local state variables
    if 'pagination' in sp or 'paging' in sp:
        if 'current_page' in sp or 'page_number' in sp or 'current' in sp:
            return 'page'
        if 'total_pages' in sp or 'page_count' in sp:
            return 'Math.ceil((data?.total ?? 0) / (data?.pageSize ?? 20))'
        if 'total_count' in sp or 'total_items' in sp or 'total' in sp:
            return 'data?.total ?? 0'
        if 'per_page' in sp or 'page_size' in sp:
            return 'data?.pageSize ?? 20'

    # .total_count or .count → data?.total ?? 0
    if sp.endswith('.total_count') or sp.endswith('.count') or sp.endswith('.total'):
        return 'data?.total ?? 0'

    # For stat-related paths in dashboard pages, use data?.stats
    if page_type == 'dashboard' and ('stats' in sp or 'stat' in sp):
        # e.g. caregiver_stats.active → data?.stats?.active ?? 0
        parts = sp.split('.')
        if len(parts) >= 2:
            return f'data?.stats?.{parts[-1]} ?? 0'
        return 'data?.stats ?? {}'

    # Default: use safe optional chaining
    return _safe_optional_chain(sp)


def _build_react_page(
    slug: str,
    recipe,
    page_name: str,
    all_imports: list[str],
    content_imports: list[str],
    content_placements: list,
    data_sources: set[str],
    loading_p: list, error_p: list, empty_p: list,
    toast_p: list, modal_p: list,
    _pascal,
) -> list[str]:
    """React/Next.js 완성형 페이지 골격 생성 (AI 0회)."""
    L = []  # noqa: E741

    # ── 파일 헤더 + import (App Router) ──
    _app_file_path = f"src/app/(main)/{slug}/page.tsx"
    L.append(f"// FILE: {_app_file_path}")
    L.append(f"// GENERATED BY: placement-assembler (DO NOT modify structure)")
    L.append(f"'use client';")
    L.append(f"")
    L.append(f"import React, {{ useState, useEffect, useCallback }} from 'react';")

    # 컴포넌트 import (UX + 콘텐츠 통합)
    imports_str = ", ".join(all_imports)
    L.append(f"import {{ {imports_str} }} from '@/components';")
    L.append(f"")

    # ── TypeScript 인터페이스 ──
    L.append(f"interface {page_name}Data {{")
    for src in sorted(data_sources) if data_sources else ["items"]:
        L.append(f"  {src}: any[];  /* AI: 실제 타입으로 교체 */")
    L.append(f"}}")
    L.append(f"")

    # ── 컴포넌트 시작 ──
    L.append(f"export default function {page_name}Page() {{")

    # ── State 선언 (확정) ──
    L.append(f"  // ── State (구조 확정 — 변경 금지) ──")
    L.append(f"  const [loading, setLoading] = useState(true);")
    L.append(f"  const [error, setError] = useState<Error | null>(null);")
    L.append(f"  const [data, setData] = useState<{page_name}Data | null>(null);")
    L.append(f"  const [toastMessage, setToastMessage] = useState<string | null>(null);")
    L.append(f"  const [modalOpen, setModalOpen] = useState(false);")
    L.append(f"")

    # ── Data Fetching (AI 구현 영역) ──
    L.append(f"  // ── Data Fetching (AI 구현 영역) ──")
    L.append(f"  const fetchData = useCallback(async () => {{")
    L.append(f"    try {{")
    L.append(f"      setLoading(true);")
    L.append(f"      setError(null);")
    L.append(f"      /* AI: API 호출 구현 */")
    L.append(f"      /* 예: const res = await fetch('/api/...'); */")
    L.append(f"      /* const json = await res.json(); */")
    L.append(f"      /* setData(json); */")
    L.append(f"    }} catch (e) {{")
    L.append(f"      setError(e instanceof Error ? e : new Error(String(e)));")
    L.append(f"    }} finally {{")
    L.append(f"      setLoading(false);")
    L.append(f"    }}")
    L.append(f"  }}, []);")
    L.append(f"")
    L.append(f"  useEffect(() => {{ fetchData(); }}, [fetchData]);")
    L.append(f"")

    # ── Event Handlers (AI 구현 영역) ──
    L.append(f"  // ── Event Handlers (AI 구현 영역) ──")
    # 반복/바인딩에서 핸들러 힌트 추출
    handler_hints = set()
    for p in content_placements:
        for b in p.bindings:
            if b.slot_name.startswith("on") or b.slot_name.startswith("handle"):
                handler_hints.add(b.slot_name)
    if not handler_hints:
        handler_hints = {"handleAction"}

    for h in sorted(handler_hints):
        fn_name = h if h.startswith("handle") else f"handle{h[2:].capitalize()}" if h.startswith("on") else h
        L.append(f"  const {fn_name} = useCallback(async (...args: any[]) => {{")
        L.append(f"    /* AI: 이벤트 핸들러 구현 */")
        L.append(f"    setToastMessage('처리 완료');")
        L.append(f"  }}, []);")
        L.append(f"")

    # ── JSX Return (구조 확정) ──
    L.append(f"  // ── Render (구조 확정 — 변경 금지) ──")

    # loading 분기
    L.append(f"  if (loading) {{")
    if loading_p:
        L.append(f"    return <LoadingIndicator />;")
    else:
        L.append(f"    return <div className=\"loading-skeleton\">로딩 중...</div>;")
    L.append(f"  }}")
    L.append(f"")

    # error 분기
    L.append(f"  if (error) {{")
    if error_p:
        L.append(f"    return <ErrorBoundary error={{error}} onRetry={{fetchData}} />;")
    else:
        L.append(f"    return <div className=\"error-fallback\">오류: {{error.message}}</div>;")
    L.append(f"  }}")
    L.append(f"")

    # empty 분기
    has_data_check = " || ".join(f"!data?.{s}?.length" for s in sorted(data_sources)) if data_sources else "!data"
    L.append(f"  if ({has_data_check}) {{")
    if empty_p:
        L.append(f"    return <EmptyState onAction={{fetchData}} />;")
    else:
        L.append(f"    return <div className=\"empty-state\">데이터가 없습니다</div>;")
    L.append(f"  }}")
    L.append(f"")

    # 메인 레이아웃
    L.append(f"  return (")
    L.append(f"    <div className=\"page-{slug} layout-{recipe.layout}\">")

    # 콘텐츠 placement 순서대로 배치
    for p in content_placements:
        pc = _pascal(p.component_name)
        cn_lower = p.component_name.lower()
        indent = "      "

        # Bug 5: detect sidebar/nav, Bug 4: pagination, Bug 3: stat card
        _is_sidebar = any(k in cn_lower for k in ("sidebar", "nav", "menu", "gnb"))
        _is_pagination = any(k in cn_lower for k in ("pagination", "pager", "page_nav"))
        _is_stat_card = any(k in cn_lower for k in ("stat_card", "statcard", "stat_grid", "stats"))

        # props 문자열 생성
        props_parts = []
        for b in p.bindings:
            # selectable without bulk_actions → skip (체크박스만 있고 일괄 처리 없으면 무의미)
            if b.slot_name == "selectable" and b.value is True:
                has_bulk = any(bb.slot_name == "bulk_actions" for bb in p.bindings)
                if not has_bulk:
                    continue

            # Bug 5: skip sidebar color props
            if _is_sidebar and b.slot_name in _SIDEBAR_COLOR_PROPS:
                continue

            # Bug 4: pagination prop overrides
            if _is_pagination:
                if b.slot_name in ("current_page", "page", "currentPage"):
                    props_parts.append(f"{b.slot_name}={{page}}")
                    continue
                elif b.slot_name in ("total_pages", "totalPages", "pageCount"):
                    props_parts.append(f"{b.slot_name}={{Math.ceil((data?.total ?? 0) / (data?.pageSize ?? 20))}}")
                    continue
                elif b.slot_name in ("per_page", "pageSize", "perPage"):
                    props_parts.append(f"{b.slot_name}={{data?.pageSize ?? 20}}")
                    continue
                elif b.slot_name in ("onPageChange", "onChange", "onPage"):
                    props_parts.append(f"{b.slot_name}={{handlePageChange}}")
                    continue

            if b.value is not None:
                if isinstance(b.value, str):
                    _href_slots = (
                        "href", "link_to", "detail_href", "detail_href_template",
                        "edit_href", "action_button_href", "action_href",
                        "create_href", "delete_href", "view_href", "url", "to",
                    )
                    if any(c in b.value for c in ["{", "["]) and b.slot_name in _href_slots:
                        _js_val = _re.sub(
                            r'\{(\w+(?:\.\w+)*)\}',
                            lambda m: '${' + m.group(1).replace('.', '?.') + '}',
                            b.value,
                        )
                        _js_val = _re.sub(
                            r'\[(\w+)\]',
                            lambda m: '${' + m.group(1) + '}',
                            _js_val,
                        )
                        props_parts.append(f"{b.slot_name}={{`{_js_val}`}}")
                    # Bug 1: template variables in non-href string values
                    elif _is_template_string(b.value):
                        jsx_expr = _convert_template_to_jsx(b.value)
                        props_parts.append(f"{b.slot_name}={jsx_expr}")
                    else:
                        props_parts.append(f'{b.slot_name}="{b.value}"')
                else:
                    # Bug 3: StatCard items with template vars
                    if isinstance(b.value, list) and _is_stat_card and b.slot_name == "items":
                        _converted = _convert_stat_items(b.value)
                        props_parts.append(f"{b.slot_name}={{{_converted}}}")
                    # Bug 6: menu_items href normalization
                    elif isinstance(b.value, list) and _is_sidebar and b.slot_name in ("menu_items", "items", "menus", "nav_items"):
                        # Note: no all_slugs available in scaffold mode, pass None
                        props_parts.append(f"{b.slot_name}={{{_py_to_js_literal(b.value)}}}")
                    else:
                        props_parts.append(f"{b.slot_name}={{{_py_to_js_literal(b.value)}}}")
            elif b.source_path:
                # Bug 2: map source_path to actual data structure
                mapped = _map_source_path_to_data(b.source_path, b.slot_name, p.component_name, "list")
                props_parts.append(f"{b.slot_name}={{{mapped}}}")
        props_str = (" " + " ".join(props_parts[:5])) if props_parts else ""

        # wrapper class
        wrapper_open = ""
        wrapper_close = ""
        if p.wrapper_css_class:
            wrapper_open = f'{indent}<div className="{p.wrapper_css_class}">'
            wrapper_close = f"{indent}</div>"
            indent = indent + "  "

        if p.condition:
            cond_var = p.condition.replace("if ", "").replace("unless ", "!").strip()
            if wrapper_open:
                L.append(wrapper_open)
            L.append(f"{indent}{{{cond_var} && (")
            L.append(f"{indent}  <{pc}{props_str} />")
            L.append(f"{indent})}}")
            if wrapper_close:
                L.append(wrapper_close)
        elif p.repeat:
            if wrapper_open:
                L.append(wrapper_open)
            L.append(f"{indent}{{{p.repeat}?.map((item, i) => (")
            L.append(f"{indent}  <{pc} key={{i}} {{...item}}{props_str} />")
            L.append(f"{indent}))}}")
            if wrapper_close:
                L.append(wrapper_close)
        else:
            if wrapper_open:
                L.append(wrapper_open)
            L.append(f"{indent}<{pc}{props_str} />")
            if wrapper_close:
                L.append(wrapper_close)

    L.append(f"")

    # Toast 포탈
    L.append(f"      {{/* ── Toast Portal (구조 확정) ── */}}")
    if toast_p:
        L.append(f"      <ToastContainer message={{toastMessage}} onClose={{() => setToastMessage(null)}} />")
    else:
        L.append(f"      {{toastMessage && <div className=\"toast\">{{toastMessage}}</div>}}")

    # Modal 포탈
    L.append(f"      {{/* ── Modal Portal (구조 확정) ── */}}")
    if modal_p:
        L.append(f"      <ModalContainer open={{modalOpen}} onClose={{() => setModalOpen(false)}}>")
        L.append(f"        {{/* AI: 모달 콘텐츠 구현 */}}")
        L.append(f"      </ModalContainer>")
    else:
        L.append(f"      {{modalOpen && <div className=\"modal-overlay\" onClick={{() => setModalOpen(false)}} />}}")

    L.append(f"    </div>")
    L.append(f"  );")
    L.append(f"}}")

    return L


def _gen_react_imports_and_types(
    page_name: str,
    page_type: str,
    resource: str,
    recipe,
    matched_table: tuple | None,
    matched_api: dict[str, dict],
    content_placements: list,
    component_path_map: dict[str, str] | None,
    _pascal,
) -> tuple[list[str], str, str, dict[str, str]]:
    """Imports + TypeScript interfaces + data interfaces + API constants.

    Returns:
        (lines, interface_name, api_base, ep_consts)
    """
    L = []  # noqa: E741
    _cpm = component_path_map or {}

    # ── Imports (workspace 실제 경로 기반) ──
    all_component_names = sorted(set(
        _pascal(p.component_name) for p in recipe.placements
    ))

    L.append("import React, { useState, useEffect, useCallback, useRef } from 'react';")
    if page_type == "form":
        L.append("import { useForm } from 'react-hook-form';")
    if page_type in ("detail", "form"):
        L.append("import { useParams, useRouter } from 'next/navigation';")
    elif page_type == "list":
        L.append("import { useRouter } from 'next/navigation';")

    # 컴포넌트 import — 실제 workspace 경로 사용, 없으면 @/components 폴백
    _imports_by_path: dict[str, list[str]] = {}
    for comp in all_component_names:
        if comp in _cpm:
            path = _cpm[comp]
        else:
            path = f"@/components/{comp}"
        _imports_by_path.setdefault(path, []).append(comp)

    for path, names in sorted(_imports_by_path.items()):
        if len(names) == 1:
            L.append(f"import {names[0]} from '{path}';")
        else:
            L.append(f"import {{ {', '.join(sorted(names))} }} from '{path}';")
    L.append("")

    # ── (5) TypeScript 인터페이스 — DB 스키마 기반 실제 타입 추론 ──
    if matched_table:
        tname, tdata = matched_table
        interface_name = _pascal(tname)
        L.append(f"interface {interface_name} {{")
        for col in tdata["columns"]:
            ts_type = _sql_type_to_ts(col["type"])
            optional = "?" if col.get("nullable") and not col.get("pk") else ""
            L.append(f"  {col['name']}{optional}: {ts_type};")
        L.append("}")
    else:
        interface_name = f"{page_name}Item"
        L.append(f"interface {interface_name} {{")
        L.append("  id: string;")
        seen_fields: dict[str, str] = {"id": "string"}
        _resp_fields: dict[str, str] = {}
        for _ep_data in matched_api.values():
            for _rf in _ep_data.get("response_fields", []):
                if isinstance(_rf, str):
                    _resp_fields[_rf] = "string"
                elif isinstance(_rf, dict):
                    _resp_fields[_rf.get("name", "")] = _rf.get("type", "string")
        for p in content_placements:
            for b in (p.bindings if hasattr(p, "bindings") else []):
                field = getattr(b, "slot_name", "") if hasattr(b, "slot_name") else ""
                if field and field not in seen_fields and not field.startswith("on"):
                    if field in _resp_fields:
                        ts_t = _resp_fields[field]
                    elif any(kw in field.lower() for kw in ("count", "total", "price", "amount", "age", "qty")):
                        ts_t = "number"
                    elif any(kw in field.lower() for kw in ("is_", "has_", "active", "enabled", "visible")):
                        ts_t = "boolean"
                    elif any(kw in field.lower() for kw in ("date", "at", "time", "created", "updated")):
                        ts_t = "string"
                    else:
                        ts_t = "string"
                    seen_fields[field] = ts_t
                    L.append(f"  {field}: {ts_t};")
        L.append("}")

    L.append("")

    # page_type별 데이터 인터페이스
    if page_type == "list":
        L.append(f"interface {page_name}Data {{")
        L.append(f"  items: {interface_name}[];")
        L.append("  total: number;")
        L.append("  page: number;")
        L.append("  pageSize: number;")
        L.append("  [key: string]: unknown;")
        L.append("}")
    elif page_type == "dashboard":
        L.append(f"interface {page_name}Data {{")
        L.append("  stats: Record<string, number>;")
        L.append(f"  recentItems: {interface_name}[];")
        L.append("  [key: string]: unknown;")
        L.append("}")
    else:
        L.append(f"interface {page_name}Data {{")
        L.append(f"  item: {interface_name};")
        L.append("  [key: string]: unknown;")
        L.append("}")
    L.append("")

    # ── (2) API 경로 상수 ──
    L.append("// API endpoints")
    api_base = f"/api/{resource.replace('-', '_')}s"
    if matched_api.get("list"):
        api_base = matched_api["list"]["path"]
    elif matched_api.get("detail"):
        _detail_path = matched_api["detail"]["path"]
        api_base = _detail_path.rsplit("/", 1)[0] if "/" in _detail_path else _detail_path

    L.append(f"const API_BASE = '{api_base}';")

    _ep_consts: dict[str, str] = {}
    for _ep_key, _ep_val in matched_api.items():
        _ep_path = _ep_val.get("path", "")
        if _ep_key == "list":
            continue
        if _ep_path and not _ep_path.startswith(api_base):
            const_name = f"API_{_ep_key.upper()}"
            L.append(f"const {const_name} = '{_ep_path}';")
            _ep_consts[_ep_key] = const_name
    L.append("")

    return L, interface_name, api_base, _ep_consts


def _gen_react_state_and_handlers(
    page_name: str,
    page_type: str,
    resource: str,
    slug: str,
    interface_name: str,
    matched_api: dict[str, dict],
    _ep_consts: dict[str, str],
    api_base: str,
) -> list[str]:
    """State declarations + fetchData + CRUD handlers + useEffect."""
    L = []  # noqa: E741

    # ── 컴포넌트 시작 ──
    L.append(f"export default function {page_name}Page() {{")

    # 라우터/파라미터
    if page_type in ("detail", "form"):
        L.append("  const params = useParams();")
        L.append("  const id = params?.id as string;")
    if page_type in ("list", "detail", "form"):
        L.append("  const router = useRouter();")
    L.append("")

    # ── State ──
    L.append("  const [loading, setLoading] = useState(true);")
    L.append("  const [error, setError] = useState<Error | null>(null);")
    L.append(f"  const [data, setData] = useState<{page_name}Data | null>(null);")
    L.append("  const [toastMessage, setToastMessage] = useState<string | null>(null);")
    L.append("  const [modalOpen, setModalOpen] = useState(false);")
    L.append("  const mountedRef = useRef(false);")
    if page_type == "list":
        L.append("  const [page, setPage] = useState(1);")
        L.append("  const [searchQuery, setSearchQuery] = useState('');")
    if page_type == "form":
        L.append(f"  const {{ register, handleSubmit, formState: {{ errors }}, reset }} = useForm<{interface_name}>();")
    L.append("")

    # ── fetchData ──
    L.append("  const fetchData = useCallback(async () => {")
    L.append("    try {")
    L.append("      setLoading(true);")
    L.append("      setError(null);")

    if page_type == "list":
        _list_ep = matched_api.get("list", {})
        _list_path = _list_ep.get("path", api_base)
        L.append(f"      const res = await fetch(`${{API_BASE}}?page=${{page}}&q=${{searchQuery}}`);")
        L.append("      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);")
        L.append("      const json = await res.json();")
        L.append("      setData({ items: json.data ?? json, total: json.total ?? 0, page, pageSize: json.pageSize ?? 20 });")
    elif page_type == "detail":
        _detail_const = _ep_consts.get("detail")
        _fetch_expr = f"${{{_detail_const}}}" if _detail_const else "${API_BASE}/${id}"
        if _detail_const:
            _detail_ep = matched_api.get("detail", {})
            _dp = _detail_ep.get("path", "")
            if ":id" in _dp or "{id}" in _dp:
                _fetch_expr = f"`{_dp.replace(':id', '${id}').replace('{id}', '${id}')}`"
                L.append(f"      const res = await fetch({_fetch_expr});")
            else:
                L.append(f"      const res = await fetch(`${{{_detail_const}}}/${{id}}`);")
        else:
            L.append("      const res = await fetch(`${API_BASE}/${id}`);")
        L.append("      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);")
        L.append("      const json = await res.json();")
        L.append("      setData({ item: json.data ?? json });")
    elif page_type == "form":
        L.append("      if (id) {")
        L.append("        const res = await fetch(`${API_BASE}/${id}`);")
        L.append("        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);")
        L.append("        const json = await res.json();")
        L.append("        setData({ item: json.data ?? json });")
        L.append("        reset(json.data ?? json);")
        L.append("      }")
    elif page_type == "dashboard":
        _stats_const = _ep_consts.get("stats")
        _stats_url = f"${{{_stats_const}}}" if _stats_const else "${API_BASE}/stats"
        L.append("      const [statsRes, recentRes] = await Promise.all([")
        L.append(f"        fetch(`{_stats_url}`),")
        L.append("        fetch(`${API_BASE}?limit=5&sort=-created_at`),")
        L.append("      ]);")
        L.append("      const stats = statsRes.ok ? await statsRes.json() : {};")
        L.append("      const recent = recentRes.ok ? await recentRes.json() : [];")
        L.append("      setData({ stats: stats.data ?? stats, recentItems: recent.data ?? recent });")

    L.append("    } catch (e) {")
    L.append("      setError(e instanceof Error ? e : new Error(String(e)));")
    L.append("    } finally {")
    L.append("      setLoading(false);")
    L.append("    }")
    if page_type == "list":
        L.append("  }, [page, searchQuery]);")
    elif page_type in ("detail", "form"):
        L.append("  }, [id]);")
    else:
        L.append("  }, []);")
    L.append("")

    # useEffect mount guard
    L.append("  useEffect(() => {")
    L.append("    if (mountedRef.current) return;")
    L.append("    mountedRef.current = true;")
    L.append("    fetchData();")
    if page_type == "list":
        L.append("  }, []);")
        L.append("")
        L.append("  // 페이지/검색 변경 시 재조회")
        L.append("  useEffect(() => {")
        L.append("    if (!mountedRef.current) return;")
        L.append("    fetchData();")
        L.append("  }, [page, searchQuery]);")
    elif page_type in ("detail", "form"):
        L.append("  }, []);")
        L.append("")
        L.append("  // id 변경 시 재조회")
        L.append("  useEffect(() => {")
        L.append("    if (!mountedRef.current) return;")
        L.append("    fetchData();")
        L.append("  }, [id]);")
    else:
        L.append("  }, []);")
    L.append("")

    # ── CRUD Handlers ──
    if page_type in ("list", "detail"):
        _delete_ep = matched_api.get("delete", {})
        _delete_path = _delete_ep.get("path", "")
        L.append("  const handleDelete = useCallback(async (targetId: string) => {")
        L.append("    if (!confirm('삭제하시겠습니까?')) return;")
        L.append("    try {")
        if _delete_path and "{" in _delete_path:
            _dp_js = _delete_path.replace("{id}", "${targetId}").replace(":id", "${targetId}")
            L.append(f"      const res = await fetch(`{_dp_js}`, {{ method: 'DELETE' }});")
        else:
            L.append("      const res = await fetch(`${API_BASE}/${targetId}`, { method: 'DELETE' });")
        L.append("      if (!res.ok) throw new Error('삭제 실패');")
        L.append("      setToastMessage('삭제되었습니다');")
        if page_type == "list":
            L.append("      fetchData();")
        else:
            L.append("      router.back();")
        L.append("    } catch (e) {")
        L.append("      setToastMessage('삭제 실패: ' + (e instanceof Error ? e.message : ''));")
        L.append("    }")
        if page_type == "detail":
            L.append("  }, [fetchData, router]);")
        else:
            L.append("  }, [fetchData]);")
        L.append("")

    if page_type == "form":
        _create_ep = matched_api.get("create", {})
        _update_ep = matched_api.get("update", {})
        is_edit = "edit" in slug or "수정" in slug
        if is_edit and _update_ep:
            _method = _update_ep.get("method", "PUT")
        elif is_edit:
            _method = "PUT"
        else:
            _method = _create_ep.get("method", "POST") if _create_ep else "POST"

        L.append(f"  const onSubmit = useCallback(async (formData: {interface_name}) => {{")
        L.append("    try {")
        if is_edit:
            if _update_ep and _update_ep.get("path"):
                _up = _update_ep["path"].replace("{id}", "${id}").replace(":id", "${id}")
                L.append(f"      const res = await fetch(`{_up}`, {{")
            else:
                L.append("      const res = await fetch(`${API_BASE}/${id}`, {")
            L.append(f"        method: '{_method}',")
        else:
            if _create_ep and _create_ep.get("path"):
                L.append(f"      const res = await fetch('{_create_ep['path']}', {{")
            else:
                L.append("      const res = await fetch(API_BASE, {")
            L.append(f"        method: '{_method}',")
        L.append("        headers: { 'Content-Type': 'application/json' },")
        L.append("        body: JSON.stringify(formData),")
        L.append("      });")
        L.append("      if (!res.ok) throw new Error('저장 실패');")
        L.append("      setToastMessage('저장되었습니다');")
        L.append("      router.back();")
        L.append("    } catch (e) {")
        L.append("      setToastMessage('저장 실패: ' + (e instanceof Error ? e.message : ''));")
        L.append("    }")
        L.append("  }, [id, router]);")
        L.append("")

    if page_type == "list":
        L.append("  const handleSearch = useCallback((q: string) => {")
        L.append("    setSearchQuery(q);")
        L.append("    setPage(1);")
        L.append("  }, []);")
        L.append("")
        L.append("  const handlePageChange = useCallback((p: number) => {")
        L.append("    setPage(p);")
        L.append("  }, []);")
        L.append("")

    return L


def _gen_react_jsx(
    slug: str,
    recipe,
    page_type: str,
    resource: str,
    content_placements: list,
    _pascal,
    component_path_map: dict[str, str] | None,
    matched_table: tuple | None,
    matched_api: dict[str, dict],
    all_slugs: list[str] | None = None,
) -> list[str]:
    """JSX render -- loading/error/empty + placements + modal + toast."""
    L = []  # noqa: E741

    # ── Render ──
    L.append("  if (loading) return <LoadingIndicator />;")
    L.append("  if (error) return <ErrorBoundary error={error} onRetry={fetchData} />;")

    if page_type == "list":
        L.append("  if (!data?.items?.length) return <EmptyState onAction={fetchData} />;")
    elif page_type == "dashboard":
        L.append("  if (!data) return <EmptyState onAction={fetchData} />;")
    elif page_type != "form":
        L.append("  if (!data?.item) return <EmptyState onAction={fetchData} />;")
    L.append("")

    # 메인 JSX
    L.append("  return (")
    L.append(f"    <div className=\"page-{slug} layout-{recipe.layout}\">")

    # 콘텐츠 placement
    for p in content_placements:
        pc = _pascal(p.component_name)
        cn_lower = p.component_name.lower()
        indent = "      "

        # ── Bug 5: Detect sidebar/nav components ──
        _is_sidebar = any(k in cn_lower for k in ("sidebar", "nav", "menu", "gnb"))
        # ── Bug 4: Detect pagination components ──
        _is_pagination = any(k in cn_lower for k in ("pagination", "pager", "page_nav"))
        # ── Bug 3: Detect stat card components ──
        _is_stat_card = any(k in cn_lower for k in ("stat_card", "statcard", "stat_grid", "stats"))

        bindings = getattr(p, "bindings", None) or []

        props_parts = []
        for b in bindings:
            slot = getattr(b, "slot_name", "") if hasattr(b, "slot_name") else ""
            if not slot:
                continue

            # ── Bug 5: Skip hardcoded sidebar color props ──
            if _is_sidebar and slot in _SIDEBAR_COLOR_PROPS:
                continue

            # ── Bug 4: Override pagination props with state variables ──
            if _is_pagination:
                if slot in ("current_page", "page", "currentPage"):
                    props_parts.append(f"{slot}={{page}}")
                    continue
                elif slot in ("total_pages", "totalPages", "pageCount"):
                    props_parts.append(f"{slot}={{Math.ceil((data?.total ?? 0) / (data?.pageSize ?? 20))}}")
                    continue
                elif slot in ("per_page", "pageSize", "perPage"):
                    props_parts.append(f"{slot}={{data?.pageSize ?? 20}}")
                    continue
                elif slot in ("total", "total_count", "totalCount"):
                    props_parts.append(f"{slot}={{data?.total ?? 0}}")
                    continue
                elif slot in ("onPageChange", "onChange", "onPage"):
                    props_parts.append(f"{slot}={{handlePageChange}}")
                    continue

            if slot.startswith("on") or slot.startswith("handle"):
                handler_name = slot
                if page_type == "list" and "delete" in handler_name.lower():
                    props_parts.append(f"{handler_name}={{handleDelete}}")
                elif page_type == "list" and "search" in handler_name.lower():
                    props_parts.append(f"{handler_name}={{handleSearch}}")
                elif page_type == "list" and "page" in handler_name.lower():
                    props_parts.append(f"{handler_name}={{handlePageChange}}")
                elif page_type == "form" and "submit" in handler_name.lower():
                    props_parts.append(f"{handler_name}={{handleSubmit(onSubmit)}}")
                elif page_type == "detail" and "delete" in handler_name.lower():
                    props_parts.append(f"{handler_name}={{handleDelete}}")
                else:
                    props_parts.append(f"{handler_name}={{() => {{}}}}")
            elif getattr(b, "value", None) is not None:
                # selectable without bulk_actions is useless — skip it
                if slot == "selectable" and b.value is True:
                    has_bulk = any(
                        getattr(_b, "slot_name", "") == "bulk_actions"
                        for _b in bindings
                    )
                    if not has_bulk:
                        continue  # selectable=true 무시 (bulk_actions 없으면 체크박스 불필요)
                if isinstance(b.value, str):
                    # href-related slots with {var} template patterns → JS template literal
                    _href_slots = (
                        "href", "link_to", "detail_href", "detail_href_template",
                        "edit_href", "action_button_href", "action_href",
                        "create_href", "delete_href", "view_href", "url", "to",
                    )
                    if any(c in b.value for c in ["{", "["]) and slot in _href_slots:
                        # Convert {field} → ${item?.field} for JS template literal
                        _js_val = _re.sub(
                            r'\{(\w+(?:\.\w+)*)\}',
                            lambda m: '${' + m.group(1).replace('.', '?.') + '}',
                            b.value,
                        )
                        # Also convert [id] to ${item?.id}
                        _js_val = _re.sub(
                            r'\[(\w+)\]',
                            lambda m: '${' + m.group(1) + '}',
                            _js_val,
                        )
                        props_parts.append(f"{slot}={{`{_js_val}`}}")

                    # ── Bug 1: Template variables in non-href string values ──
                    elif _is_template_string(b.value):
                        jsx_expr = _convert_template_to_jsx(b.value)
                        props_parts.append(f"{slot}={jsx_expr}")

                    # ── Bug 6: Normalize sidebar/nav href values ──
                    elif _is_sidebar and slot in _href_slots:
                        fixed_href = _normalize_sidebar_href(b.value, all_slugs)
                        props_parts.append(f'{slot}="{fixed_href}"')

                    else:
                        # ── Bug 3: StatCard items with template vars in nested structures ──
                        if isinstance(b.value, str) and _is_stat_card and slot in ("value", "count"):
                            if _is_template_string(b.value):
                                jsx_expr = _convert_template_to_jsx(b.value)
                                props_parts.append(f"{slot}={jsx_expr}")
                                continue
                        props_parts.append(f'{slot}="{b.value}"')
                else:
                    # ── Bug 3: Handle list/dict values with template vars (StatCard items) ──
                    if isinstance(b.value, list) and _is_stat_card and slot == "items":
                        _converted_items = _convert_stat_items(b.value)
                        props_parts.append(f"{slot}={{{_converted_items}}}")
                    # ── Bug 6: Normalize hrefs inside menu_items lists ──
                    elif isinstance(b.value, list) and _is_sidebar and slot in ("menu_items", "items", "menus", "nav_items"):
                        _fixed_items = _fix_menu_items_hrefs(b.value, all_slugs)
                        props_parts.append(f"{slot}={{{_py_to_js_literal(_fixed_items)}}}")
                    else:
                        props_parts.append(f"{slot}={{{_py_to_js_literal(b.value)}}}")
            elif getattr(b, "source_path", ""):
                sp = b.source_path
                # ── Bug 2: Map source_path to actual data structure ──
                mapped = _map_source_path_to_data(sp, slot, p.component_name, page_type)
                props_parts.append(f"{slot}={{{mapped}}}")
            else:
                if page_type == "list":
                    props_parts.append(f"{slot}={{data?.items}}")
                elif page_type == "dashboard":
                    props_parts.append(f"{slot}={{data?.stats}}")
                else:
                    props_parts.append(f"{slot}={{data?.item?.{slot}}}")

        if len(props_parts) <= 3:
            props_str = (" " + " ".join(props_parts)) if props_parts else ""
        else:
            props_str = ""

        # wrapper
        w_open = w_close = ""
        actual_indent = indent
        if p.wrapper_css_class:
            w_open = f'{indent}<div className="{p.wrapper_css_class}">'
            w_close = f"{indent}</div>"
            actual_indent = indent + "  "

        condition = getattr(p, "condition", "") or ""
        repeat = getattr(p, "repeat", "") or ""

        if condition:
            cond_var = _py_condition_to_jsx(condition)
            if w_open:
                L.append(w_open)
            if props_str:
                L.append(f"{actual_indent}{{{cond_var} && <{pc}{props_str} />}}")
            else:
                L.append(f"{actual_indent}{{{cond_var} && (")
                L.append(f"{actual_indent}  <{pc}")
                for pp in props_parts:
                    L.append(f"{actual_indent}    {pp}")
                L.append(f"{actual_indent}  />")
                L.append(f"{actual_indent})}}")
            if w_close:
                L.append(w_close)
        elif repeat:
            repeat_src = _py_data_path_to_js(repeat)
            if w_open:
                L.append(w_open)
            L.append(f"{actual_indent}{{{repeat_src}?.map((item, i) => (")
            if props_str:
                L.append(f"{actual_indent}  <{pc} key={{item.id ?? i}} {{...item}}{props_str} />")
            else:
                L.append(f"{actual_indent}  <{pc}")
                L.append(f"{actual_indent}    key={{item.id ?? i}}")
                L.append(f"{actual_indent}    {{...item}}")
                for pp in props_parts:
                    L.append(f"{actual_indent}    {pp}")
                L.append(f"{actual_indent}  />")
            L.append(f"{actual_indent}))}}")
            if w_close:
                L.append(w_close)
        else:
            if w_open:
                L.append(w_open)
            if not props_parts:
                auto_str = _auto_props_for_component(
                    p.component_name, page_type, resource,
                )
                L.append(f"{actual_indent}<{pc}{auto_str} />")
            elif props_str:
                L.append(f"{actual_indent}<{pc}{props_str} />")
            else:
                L.append(f"{actual_indent}<{pc}")
                for pp in props_parts:
                    L.append(f"{actual_indent}  {pp}")
                L.append(f"{actual_indent}/>")
            if w_close:
                L.append(w_close)

    L.append("")

    # Modal + Toast
    L.append("      <ToastContainer message={toastMessage} onClose={() => setToastMessage(null)} />")
    if page_type == "list":
        L.append("      {modalOpen && (")
        L.append("        <div className=\"modal-overlay\" onClick={() => setModalOpen(false)}>")
        L.append("          <div className=\"modal-content\" onClick={(e) => e.stopPropagation()}>")
        L.append("            <button className=\"modal-close\" onClick={() => setModalOpen(false)}>×</button>")
        L.append(f"            <h2>{resource} 상세</h2>")
        L.append("            {/* TODO: AI가 상세 콘텐츠 구현 */}")
        L.append("          </div>")
        L.append("        </div>")
        L.append("      )}")
    elif page_type == "detail":
        L.append("      {modalOpen && (")
        L.append("        <div className=\"modal-overlay\" onClick={() => setModalOpen(false)}>")
        L.append("          <div className=\"modal-content\" onClick={(e) => e.stopPropagation()}>")
        L.append("            <button className=\"modal-close\" onClick={() => setModalOpen(false)}>×</button>")
        L.append(f"            <h2>{resource} 수정</h2>")
        if matched_table:
            _editable_cols = [c for c in matched_table[1]["columns"] if not c.get("pk") and c["name"] not in ("created_at", "updated_at")]
            for _col in _editable_cols[:10]:
                _input_type = "number" if _sql_type_to_ts(_col["type"]) == "number" else "text"
                L.append(f"            <label>{_col['name']}</label>")
                L.append(f"            <input type=\"{_input_type}\" defaultValue={{data?.item?.{_col['name']} ?? ''}} />")
        L.append("            <button onClick={() => setModalOpen(false)}>저장</button>")
        L.append("          </div>")
        L.append("        </div>")
        L.append("      )}")
    else:
        L.append("      {modalOpen && (")
        L.append("        <div className=\"modal-overlay\" onClick={() => setModalOpen(false)}>")
        L.append("          <div className=\"modal-content\" onClick={(e) => e.stopPropagation()}>")
        L.append("            <button className=\"modal-close\" onClick={() => setModalOpen(false)}>×</button>")
        L.append("          </div>")
        L.append("        </div>")
        L.append("      )}")

    L.append("    </div>")
    L.append("  );")
    L.append("}")

    return L


def _generate_react_complete(
    slug: str,
    recipe,
    page_name: str,
    page_type: str,
    resource: str,
    content_placements: list,
    matched_api: dict[str, dict],
    matched_table: tuple | None,
    tokens: dict,
    _pascal,
    component_path_map: dict[str, str] | None = None,
    all_slugs: list[str] | None = None,
) -> str:
    """완전한 React 페이지 코드 생성 (AI 0회).

    page_type별 CRUD 패턴:
      - list: GET 목록 + 삭제 + 페이지네이션
      - detail: GET 단건 + 수정/삭제
      - form: POST/PUT + 폼 검증
      - dashboard: 다중 GET + 통계 집계
    """
    # ── (9) API/DB spec 빈값 경고 ──
    if not matched_api:
        logger.warning(
            "react_complete_no_api_match slug=%s resource=%s — 기본 CRUD 경로 사용",
            slug, resource,
        )
    if not matched_table:
        logger.warning(
            "react_complete_no_db_match slug=%s resource=%s — 바인딩 기반 인터페이스 사용",
            slug, resource,
        )

    # ── 파일 헤더 ──
    _app_file_path = f"src/app/(main)/{slug}/page.tsx"
    L = [  # noqa: E741
        f"// FILE: {_app_file_path}",
        f"// GENERATED BY: programmatic-assembler (AI 0 calls)",
        f"// PAGE TYPE: {page_type} | RESOURCE: {resource}",
        "'use client';",
        "",
    ]

    # Phase 1: Imports + TypeScript interfaces + API constants
    import_lines, interface_name, api_base, _ep_consts = _gen_react_imports_and_types(
        page_name, page_type, resource, recipe,
        matched_table, matched_api, content_placements,
        component_path_map, _pascal,
    )
    L += import_lines

    # Phase 2: State + handlers (also opens the component function)
    L += _gen_react_state_and_handlers(
        page_name, page_type, resource, slug,
        interface_name, matched_api, _ep_consts, api_base,
    )

    # Phase 3: JSX render (also closes the component function)
    L += _gen_react_jsx(
        slug, recipe, page_type, resource, content_placements,
        _pascal, component_path_map, matched_table, matched_api,
        all_slugs=all_slugs,
    )

    return "\n".join(L)
