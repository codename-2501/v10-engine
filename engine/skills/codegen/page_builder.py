"""
Page Builder — placement-based programmatic code scaffold generation.

Extracted from executor.py. Contains:
  - _build_placement_scaffold
  - _build_complete_page_code
  - _build_binding_summary
"""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

from engine.skills.artifact.loader import (
    _load_api_spec_parsed,
    _load_db_spec_parsed,
    _build_component_path_map,
)
from engine.skills.codegen.react import _build_react_page, _generate_react_complete
from engine.skills.codegen.vue import _build_vue_page
from engine.skills.codegen.generic import _build_generic_page
from engine.skills.codegen.helpers import (
    _slug_to_resource,
    _match_endpoints_for_page,
    _detect_page_type,
)

logger = logging.getLogger(__name__)


async def _build_placement_scaffold(
    db: Any, project_id: str, page_slugs: list[str] | None, platform: dict,
) -> tuple[str, dict[str, str]]:
    """레시피 placement → 완성형 React/Vue 페이지 골격 프로그래매틱 생성 (AI 0회).

    HTML 조립(renderer.py)과 동일 패턴: placement 순서대로 코드 조립.
    생성되는 골격은 **즉시 실행 가능한 수준**이며, AI는 아래만 보강:
      1. useEffect 내 data fetching 로직 (API 엔드포인트 + 응답 매핑)
      2. 이벤트 핸들러 구현 (onClick, onSubmit 등)
      3. 비즈니스 로직 상태 변환 (필터, 정렬, 검증 등)

    구조(컴포넌트 배치·import·ErrorBoundary·loading/empty 분기·toast/modal 포탈)는
    이 함수가 **확정**하므로 AI가 변경하면 안 됨.

    Returns:
        (prompt_text, scaffold_code_map):
          prompt_text — 프롬프트용 텍스트 (구 방식 호환)
          scaffold_code_map — {slug: 코드문자열} 매핑 (scaffold-first 파이프라인용)
    """
    from engine.composition.registry import (
        REQUIRED_UX_PLACEMENTS,
        ensure_required_placements,
        _dict_to_recipe,
    )

    recipes_rows = await db.fetchall(
        "SELECT page_slug, page_name, data FROM composition_recipes WHERE project_id=?",
        (project_id,),
    )
    if not recipes_rows:
        return ("", {})

    framework = platform.get("framework", "Next.js + React")
    is_react = "react" in framework.lower() or "next" in framework.lower()
    is_vue = "vue" in framework.lower()

    # PascalCase 변환 (클로저 밖으로 추출)
    def _pascal(name: str) -> str:
        return "".join(w.capitalize() for w in name.replace("-", "_").split("_"))

    # scaffold_code_map: slug → 코드 문자열 (scaffold-first 파이프라인용)
    scaffold_code_map: dict[str, str] = {}

    parts = [
        "\n\n## 🏗️ Placement 기반 완성 골격 (프로그래매틱 조립 — 구조 변경 금지)\n",
        "아래 코드는 레시피 placement에서 **프로그래매틱 조립**된 완성 골격입니다.\n"
        "**구조(컴포넌트 배치·import·분기·포탈)를 변경하지 마세요.**\n"
        "AI가 채울 부분은 `/* AI: ... */` 주석으로 명시되어 있습니다.\n\n"
        "### AI 담당 영역 (이것만 구현)\n"
        "1. `fetchData()` — API 호출 + 응답 매핑\n"
        "2. `handle*()` 이벤트 핸들러 — 사용자 인터랙션 처리\n"
        "3. 비즈니스 로직 — 필터, 정렬, 폼 검증, 상태 변환\n\n"
        "### AI 금지 영역 (절대 변경 불가)\n"
        "- 컴포넌트 배치 순서 및 import 구조\n"
        "- ErrorBoundary / loading / empty_state 분기 구조\n"
        "- toast_container / modal_container 포탈 위치\n"
        "- 파일명 및 컴포넌트 함수명\n",
    ]

    for row in recipes_rows:
        slug = row["page_slug"]
        if page_slugs and slug not in page_slugs:
            continue

        try:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (ValueError, TypeError):
            continue

        recipe = _dict_to_recipe({"page_slug": slug, "page_name": row["page_name"], **data})
        recipe = ensure_required_placements(recipe)

        sorted_placements = sorted(recipe.placements, key=lambda p: p.order)

        # placement를 역할별 분류
        ux_names = {r["component_name"] for r in REQUIRED_UX_PLACEMENTS}
        loading_p = [p for p in sorted_placements if p.component_name == "loading_indicator"]
        error_p = [p for p in sorted_placements if p.component_name == "error_boundary"]
        empty_p = [p for p in sorted_placements if p.component_name == "empty_state"]
        toast_p = [p for p in sorted_placements if p.component_name == "toast_container"]
        modal_p = [p for p in sorted_placements if p.component_name == "modal_container"]
        content_placements = [p for p in sorted_placements if p.component_name not in ux_names]

        # 바인딩에서 데이터 소스 추출 (API 힌트용)
        data_sources = set()
        for p in content_placements:
            if p.repeat:
                data_sources.add(p.repeat)
            for b in p.bindings:
                if b.source_path:
                    data_sources.add(b.source_path.split("[")[0].split(".")[0])

        all_component_names = sorted(set(
            _pascal(p.component_name) for p in sorted_placements
        ))
        content_component_names = sorted(set(
            _pascal(p.component_name) for p in content_placements
        ))

        page_name_pascal = _pascal(slug)

        if is_react:
            lines = _build_react_page(
                slug, recipe, page_name_pascal,
                all_component_names, content_component_names,
                content_placements, data_sources,
                loading_p, error_p, empty_p, toast_p, modal_p,
                _pascal,
            )
        elif is_vue:
            lines = _build_vue_page(
                slug, recipe, page_name_pascal,
                all_component_names, content_component_names,
                content_placements, data_sources,
                loading_p, error_p, empty_p, toast_p, modal_p,
                _pascal,
            )
        else:
            lines = _build_generic_page(slug, recipe, sorted_placements, _pascal)

        code_str = "\n".join(lines)
        scaffold_code_map[slug] = code_str

        parts.append(f"\n### {row['page_name']} (`{slug}`)")
        parts.append(f"```tsx\n{code_str}\n```")

    page_count = len([r for r in recipes_rows if not page_slugs or r["page_slug"] in page_slugs])
    logger.info(
        "placement_scaffold_built project=%s pages=%d framework=%s mode=programmatic",
        project_id, page_count, framework,
    )
    return ("\n".join(parts), scaffold_code_map)


async def _build_complete_page_code(
    db: Any,
    project_id: str,
    page_slugs: list[str] | None,
    platform: dict,
) -> dict[str, str]:
    """레시피 + API 설계서 + DB 설계서 + 디자인 토큰으로 완전한 React 페이지 코드를 AI 0회 생성.

    HTML 조립(renderer.py)과 완전 동일 패턴:
      입력: 설계 산출물 (JSON/마크다운)
      출력: 완성된 코드 (// FILE: 구분)
      AI 호출: 0회

    생성 범위:
      - TypeScript 인터페이스 (DB 스키마 기반)
      - fetchData + CRUD 핸들러 (API 엔드포인트 기반)
      - 컴포넌트 배치 + 조건부/반복 렌더링 (레시피 기반)
      - CSS 변수 참조 스타일링 (토큰 기반)
      - loading/error/empty 분기 (필수 UX placement)

    복잡한 비즈니스 로직 → 기본 CRUD만 생성, 사람 확인 게이트에서 보완.

    Returns:
        {slug: "완전한 코드 문자열"} 매핑. 빈 dict면 레시피 없음.
    """
    from engine.composition.registry import (
        REQUIRED_UX_PLACEMENTS,
        ensure_required_placements,
        _dict_to_recipe,
    )

    # ── 데이터 소스 로드 ──
    recipes_rows = await db.fetchall(
        "SELECT page_slug, page_name, data FROM composition_recipes WHERE project_id=?",
        (project_id,),
    )
    if not recipes_rows:
        return {}

    api_spec = await _load_api_spec_parsed(db, project_id)
    db_spec = await _load_db_spec_parsed(db, project_id)

    # 디자인 토큰 로드
    token_row = await db.fetchone(
        "SELECT data FROM composition_tokens WHERE project_id=?",
        (project_id,),
    )
    tokens = {}
    if token_row:
        try:
            tokens = json.loads(token_row["data"]) if isinstance(token_row["data"], str) else token_row["data"]
        except (ValueError, TypeError):
            pass

    # ── workspace 컴포넌트 경로 맵 구축 ──
    component_path_map = await _build_component_path_map(db, project_id)

    framework = platform.get("framework", "Next.js + React")
    is_react = "react" in framework.lower() or "next" in framework.lower()

    def _pascal(name: str) -> str:
        return "".join(w.capitalize() for w in name.replace("-", "_").split("_"))

    result: dict[str, str] = {}

    # Collect all known page slugs for route normalization (Bug 6)
    all_slugs = [r["page_slug"] for r in recipes_rows]

    for row in recipes_rows:
        slug = row["page_slug"]
        if page_slugs and slug not in page_slugs:
            continue

        try:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (ValueError, TypeError):
            continue

        recipe = _dict_to_recipe({"page_slug": slug, "page_name": row["page_name"], **data})
        recipe = ensure_required_placements(recipe)
        sorted_placements = sorted(recipe.placements, key=lambda p: p.order)

        ux_names = {r["component_name"] for r in REQUIRED_UX_PLACEMENTS}
        content_placements = [p for p in sorted_placements if p.component_name not in ux_names]

        page_type = _detect_page_type(slug, content_placements)
        page_pascal = _pascal(slug)
        resource = _slug_to_resource(slug)
        matched_api = _match_endpoints_for_page(slug, api_spec["endpoints"], page_type)

        # DB 테이블 매칭
        matched_table = None
        for tname, tdata in db_spec["tables"].items():
            if resource.replace("-", "") in tname.replace("_", ""):
                matched_table = (tname, tdata)
                break

        if is_react:
            code = _generate_react_complete(
                slug, recipe, page_pascal, page_type, resource,
                content_placements, matched_api, matched_table,
                tokens, _pascal, component_path_map,
                all_slugs=all_slugs,
            )
        else:
            # 비-React 폴백: 구조 주석만
            code = "\n".join(_build_generic_page(slug, recipe, sorted_placements, _pascal))

        result[slug] = code

    # ── 2nd pass: sub-page generation from href bindings ──
    sub_pages = _generate_sub_pages_from_bindings(recipes_rows, result, framework, platform)
    for sub_slug, sub_code in sub_pages.items():
        if sub_slug not in result:
            result[sub_slug] = sub_code

    logger.info(
        "complete_page_code_built project=%s pages=%d sub_pages=%d api_endpoints=%d db_tables=%d",
        project_id, len(result), len(sub_pages), len(api_spec["endpoints"]), len(db_spec["tables"]),
    )
    return result


def _build_binding_summary(
    scaffold_code: dict[str, str],
    assembled_html: str | None,
) -> str:
    """scaffold-first 모드용 경량 바인딩 요약.

    HTML 전체(수만 자)를 프롬프트에 넣는 대신:
    - 페이지별 데이터 소스 목록 (repeat/binding에서 추출)
    - HTML에서 추출한 API 엔드포인트 힌트
    - 각 컴포넌트가 필요로 하는 데이터 필드 목록
    → AI가 fetchData 구현에 필요한 최소 컨텍스트만 제공 (~1-2K)
    """
    parts = ["\n\n## 📋 데이터 바인딩 요약 (fetchData 구현 참조)\n"]

    # scaffold에서 데이터 소스 추출
    for slug in sorted(scaffold_code):
        code = scaffold_code[slug]
        page_name = ""
        data_refs = set()
        component_props: dict[str, list[str]] = {}

        for line in code.split("\n"):
            stripped = line.strip()

            # 파일명
            if stripped.startswith("// FILE:"):
                page_name = stripped.replace("// FILE:", "").strip()

            # data?.xxx 참조 추출
            for m in _re.finditer(r'data\?\.\w+(?:\.\w+)*', stripped):
                data_refs.add(m.group())

            # 컴포넌트 props 추출 (<Component prop={data?.xxx} />)
            comp_match = _re.match(r'<(\w+)\s', stripped)
            if comp_match:
                comp_name = comp_match.group(1)
                prop_matches = _re.findall(r'(\w+)=\{data\?\.([\w.]+)\}', stripped)
                if prop_matches:
                    component_props.setdefault(comp_name, []).extend(
                        f"{p}←data.{v}" for p, v in prop_matches
                    )

            # repeat 소스 추출
            repeat_match = _re.search(r'(\w+(?:\.\w+)*)\?\.map\(', stripped)
            if repeat_match:
                data_refs.add(f"data.{repeat_match.group(1)}")

        if data_refs or component_props:
            parts.append(f"\n### `{page_name or slug}`")
            if data_refs:
                parts.append(f"**필요 데이터:** {', '.join(sorted(data_refs))}")
            if component_props:
                for comp, props in sorted(component_props.items()):
                    parts.append(f"- `<{comp}>`: {', '.join(props[:5])}")

    # HTML에서 API 엔드포인트 힌트 추출 (fetch/axios 패턴)
    if assembled_html:
        api_hints = set()
        for m in _re.finditer(r'(?:fetch|axios\.\w+)\s*\(\s*["\']([^"\']+)["\']', assembled_html):
            api_hints.add(m.group(1))
        # data-api 속성
        for m in _re.finditer(r'data-api=["\']([^"\']+)["\']', assembled_html):
            api_hints.add(m.group(1))
        if api_hints:
            parts.append(f"\n**API 엔드포인트 힌트:** {', '.join(sorted(api_hints))}")

    # HTML 전체가 아닌 요약만 제공하는 이유 명시
    parts.append(
        "\n\n> 위 바인딩 요약을 참고하여 `fetchData()`에서 적절한 API를 호출하고 "
        "응답을 `setData()`에 매핑하세요.\n"
    )

    return "\n".join(parts)


# ============================================================
# Sub-page generation from href bindings
# ============================================================

# Korean label map for resource names in sub-pages
_RESOURCE_LABEL_MAP = {
    "clients": "이용자", "client": "이용자",
    "caregivers": "요양보호사", "caregiver": "요양보호사",
    "staff": "직원", "staffs": "직원",
    "employees": "직원", "employee": "직원",
    "patients": "환자", "patient": "환자",
    "elders": "어르신", "elder": "어르신",
    "users": "사용자", "user": "사용자",
    "members": "회원", "member": "회원",
    "hospitals": "병원", "hospital": "병원",
    "products": "상품", "product": "상품",
    "orders": "주문", "order": "주문",
    "schedules": "일정", "schedule": "일정",
    "reports": "보고서", "report": "보고서",
    "notices": "공지사항", "notice": "공지사항",
    "programs": "프로그램", "program": "프로그램",
    "services": "서비스", "service": "서비스",
    "facilities": "시설", "facility": "시설",
    "rooms": "호실", "room": "호실",
    "meals": "식단", "meal": "식단",
    "medications": "투약", "medication": "투약",
    "assessments": "평가", "assessment": "평가",
    "visits": "방문", "visit": "방문",
    "documents": "문서", "document": "문서",
    "settings": "설정",
    "categories": "카테고리", "category": "카테고리",
    "reviews": "리뷰", "review": "리뷰",
    "bookings": "예약", "booking": "예약",
}


def _infer_korean_label(resource_segment: str) -> str:
    """Infer Korean label from a URL resource segment. Universal, no project hardcoding."""
    seg = resource_segment.lower().strip("-_ ")
    if seg in _RESOURCE_LABEL_MAP:
        return _RESOURCE_LABEL_MAP[seg]
    # Try singular/plural heuristic
    if seg.endswith("s") and seg[:-1] in _RESOURCE_LABEL_MAP:
        return _RESOURCE_LABEL_MAP[seg[:-1]]
    # Fallback: use the segment itself in title case
    return resource_segment.replace("-", " ").replace("_", " ").title()


def _generate_sub_pages_from_bindings(
    recipes_rows: list,
    existing_pages: dict[str, str],
    framework: str,
    platform: dict,
) -> dict[str, str]:
    """Scan all recipe bindings for href patterns and generate missing sub-pages.

    Detects patterns like:
      /xxx/create, /xxx/new      → form (create) page
      /xxx/{id}, /xxx/[id]       → detail page
      /xxx/{id}/edit             → edit form page

    Returns: {slug: code_string} for each missing sub-page.
    """
    is_react = "react" in framework.lower() or "next" in framework.lower()
    if not is_react:
        return {}  # Only React/Next.js sub-page generation for now

    _href_slots = {
        "href", "link_to", "detail_href", "detail_href_template",
        "edit_href", "action_button_href", "action_href",
        "create_href", "delete_href", "view_href", "url", "to",
    }

    detected_hrefs: set[str] = set()

    for row in recipes_rows:
        try:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (ValueError, TypeError):
            continue
        placements = data.get("placements", [])
        for pl in placements:
            for b in pl.get("bindings", []):
                slot = b.get("slot_name", "")
                val = b.get("value", "")
                if not isinstance(val, str):
                    continue
                if slot in _href_slots or "href" in slot.lower():
                    if val.startswith("/"):
                        detected_hrefs.add(val)

    # Also scan generated code for router.push patterns
    for slug, code in existing_pages.items():
        for m in _re.finditer(r'(?:href|router\.push)\s*(?:=\s*[{"`\']|[\(`])\s*["`\'`]?([/][a-zA-Z0-9_/{}\[\]-]+)', code):
            detected_hrefs.add(m.group(1))

    result: dict[str, str] = {}

    for href in sorted(detected_hrefs):
        # Normalize: strip template literal markers
        clean = href.replace("${", "{").replace("`", "").strip()
        if not clean.startswith("/"):
            continue

        # Determine sub-page type
        segments = [s for s in clean.strip("/").split("/") if s]
        if not segments:
            continue

        # Detect patterns
        is_create = segments[-1] in ("create", "new", "add")
        is_edit = segments[-1] == "edit" and len(segments) >= 2
        has_id_param = any(
            s.startswith("{") or s.startswith("[") for s in segments
        )
        is_detail = has_id_param and not is_edit

        if not (is_create or is_edit or is_detail):
            continue

        # Build Next.js route slug: convert {id} → [id], 부모 경로 내 중복 슬러그
        # 방지를 위해 route_slug 유틸을 경유.
        from engine.workspace.route_slug import normalize_dynamic_segment

        route_segments: list[str] = []
        for s in segments:
            if s.startswith("{") and s.endswith("}"):
                hint = s[1:-1].split(".")[-1]  # {client.id} → id
                if hint == "id" or not hint:
                    # 부모에 이미 [id]가 있으면 유니크 슬러그로
                    parent_purepath = "/".join(route_segments)
                    static_parents = [
                        p for p in route_segments if not p.startswith("[")
                    ]
                    resource_hint = static_parents[-1] if static_parents else "id"
                    route_segments.append(
                        normalize_dynamic_segment(parent_purepath, resource_hint)
                    )
                else:
                    route_segments.append(f"[{hint}]")
            elif s.startswith("[") and s.endswith("]"):
                route_segments.append(s)
            else:
                route_segments.append(s)

        route_slug = "/".join(route_segments)

        # Check if this page already exists in generated pages
        if route_slug in existing_pages or route_slug in result:
            continue
        # Check common slug variations
        alt_slug = "-".join(route_segments)
        if alt_slug in existing_pages or alt_slug in result:
            continue

        # Infer resource name and Korean label from URL
        resource_segments = [s for s in segments if not s.startswith("{") and not s.startswith("[") and s not in ("create", "new", "add", "edit")]
        resource_name = resource_segments[-1] if resource_segments else "item"
        parent_route_parts = []
        for s in segments:
            if s.startswith("{") or s.startswith("[") or s in ("create", "new", "add", "edit"):
                break
            parent_route_parts.append(s)
        parent_route = "/" + "/".join(parent_route_parts)
        korean_label = _infer_korean_label(resource_name)

        # Build file path
        nextjs_path = "src/app/(main)/" + "/".join(route_segments) + "/page.tsx"

        if is_create:
            code = _gen_sub_page_create(nextjs_path, resource_name, korean_label, parent_route)
        elif is_edit:
            code = _gen_sub_page_edit(nextjs_path, resource_name, korean_label, parent_route)
        elif is_detail:
            code = _gen_sub_page_detail(nextjs_path, resource_name, korean_label, parent_route)
        else:
            continue

        result[route_slug] = code

    if result:
        logger.info("sub_pages_from_bindings generated=%d slugs=%s", len(result), list(result.keys()))

    return result


def _gen_sub_page_detail(file_path: str, resource: str, label: str, parent_route: str) -> str:
    """Generate a detail sub-page for Next.js."""
    api_path = f"/api/{resource.replace('-', '_')}"
    pascal = "".join(w.capitalize() for w in resource.replace("-", "_").split("_"))
    return (
        f"// FILE: {file_path}\n"
        f"// GENERATED BY: sub-page-generator (from href binding)\n"
        f"// PAGE TYPE: detail | RESOURCE: {resource}\n"
        "'use client';\n\n"
        "import React, { useState, useEffect, useCallback, useRef } from 'react';\n"
        "import { useParams, useRouter } from 'next/navigation';\n\n"
        f"interface {pascal}Item {{\n"
        "  id: string;\n"
        "  name?: string;\n"
        "  title?: string;\n"
        "  status?: string;\n"
        "  description?: string;\n"
        "  created_at?: string;\n"
        "  [key: string]: any;\n"
        "}\n\n"
        f"export default function {pascal}DetailPage() {{\n"
        "  const params = useParams();\n"
        "  const id = params?.id as string;\n"
        "  const router = useRouter();\n"
        f"  const [data, setData] = useState<{pascal}Item | null>(null);\n"
        "  const [loading, setLoading] = useState(true);\n"
        "  const [error, setError] = useState<Error | null>(null);\n\n"
        "  const fetchData = useCallback(async () => {\n"
        "    try {\n"
        "      setLoading(true);\n"
        f"      const res = await fetch(`{api_path}/${{id}}`);\n"
        "      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);\n"
        "      const json = await res.json();\n"
        "      setData(json.data ?? json);\n"
        "    } catch (e) {\n"
        "      setError(e instanceof Error ? e : new Error(String(e)));\n"
        "    } finally {\n"
        "      setLoading(false);\n"
        "    }\n"
        "  }, [id]);\n\n"
        "  useEffect(() => { fetchData(); }, [fetchData]);\n\n"
        f"  if (loading) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>로딩중...</div>;\n"
        f"  if (error) return <div style={{{{ textAlign: 'center', padding: '2rem', color: 'red' }}}}>오류: {{error.message}}</div>;\n"
        f"  if (!data) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>데이터가 없습니다</div>;\n\n"
        "  return (\n"
        f"    <div style={{{{ display: 'flex', flexDirection: 'column', gap: '1.5rem', padding: '1rem' }}}}>\n"
        f"      <header style={{{{ display: 'flex', alignItems: 'center', gap: '1rem' }}}}>\n"
        f"        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '0.875rem', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>\n"
        f"        <h1 style={{{{ fontSize: '1.5rem', fontWeight: 800 }}}}>{label} 상세</h1>\n"
        "      </header>\n"
        f"      <div style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: '1.5rem', boxShadow: '0 1px 3px rgba(0,0,0,0.1)' }}}}>\n"
        "        <h2 style={{ fontSize: '1.25rem', fontWeight: 700, marginBottom: '1rem' }}>\n"
        "          {data.name || data.title || '상세 정보'}\n"
        "        </h2>\n"
        "        {data.description && <p style={{ color: 'var(--text-secondary, #555)', marginBottom: '1rem' }}>{data.description}</p>}\n"
        "        {data.status && <p style={{ fontSize: '0.875rem' }}>상태: {data.status}</p>}\n"
        "        {data.created_at && <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary, #999)', marginTop: '1rem' }}>등록일: {new Date(data.created_at).toLocaleDateString('ko-KR')}</p>}\n"
        f"        <div style={{{{ marginTop: '1.5rem', display: 'flex', gap: '0.5rem' }}}}>\n"
        f"          <button onClick={{() => router.push(`{parent_route}/${{id}}/edit`)}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>수정</button>\n"
        f"          <button onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>목록으로</button>\n"
        "        </div>\n"
        "      </div>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def _gen_sub_page_create(file_path: str, resource: str, label: str, parent_route: str) -> str:
    """Generate a create form sub-page for Next.js."""
    api_path = f"/api/{resource.replace('-', '_')}"
    pascal = "".join(w.capitalize() for w in resource.replace("-", "_").split("_"))
    return (
        f"// FILE: {file_path}\n"
        f"// GENERATED BY: sub-page-generator (from href binding)\n"
        f"// PAGE TYPE: form | RESOURCE: {resource}\n"
        "'use client';\n\n"
        "import React, { useState, useCallback } from 'react';\n"
        "import { useRouter } from 'next/navigation';\n\n"
        f"export default function {pascal}CreatePage() {{\n"
        "  const router = useRouter();\n"
        "  const [formData, setFormData] = useState<Record<string, any>>({});\n"
        "  const [submitting, setSubmitting] = useState(false);\n"
        "  const [error, setError] = useState<string | null>(null);\n\n"
        "  const handleChange = useCallback((field: string, value: any) => {\n"
        "    setFormData(prev => ({ ...prev, [field]: value }));\n"
        "  }, []);\n\n"
        "  const handleSubmit = useCallback(async (e: React.FormEvent) => {\n"
        "    e.preventDefault();\n"
        "    try {\n"
        "      setSubmitting(true);\n"
        "      setError(null);\n"
        f"      const res = await fetch('{api_path}', {{\n"
        "        method: 'POST',\n"
        "        headers: { 'Content-Type': 'application/json' },\n"
        "        body: JSON.stringify(formData),\n"
        "      });\n"
        "      if (!res.ok) throw new Error('저장에 실패했습니다');\n"
        f"      router.push('{parent_route}');\n"
        "    } catch (e) {\n"
        "      setError(e instanceof Error ? e.message : '오류가 발생했습니다');\n"
        "    } finally {\n"
        "      setSubmitting(false);\n"
        "    }\n"
        "  }, [formData, router]);\n\n"
        "  return (\n"
        f"    <div style={{{{ display: 'flex', flexDirection: 'column', gap: '1.5rem', padding: '1rem' }}}}>\n"
        f"      <header style={{{{ display: 'flex', alignItems: 'center', gap: '1rem' }}}}>\n"
        f"        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '0.875rem', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>\n"
        f"        <h1 style={{{{ fontSize: '1.5rem', fontWeight: 800 }}}}>{label} 등록</h1>\n"
        "      </header>\n"
        "      {error && <div style={{ padding: '0.75rem', background: '#fef2f2', color: '#dc2626', borderRadius: '0.375rem' }}>{error}</div>}\n"
        f"      <form onSubmit={{handleSubmit}} style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: '1.5rem', boxShadow: '0 1px 3px rgba(0,0,0,0.1)', display: 'flex', flexDirection: 'column', gap: '1rem' }}}}>\n"
        f"        <div>\n"
        f"          <label style={{{{ display: 'block', fontSize: '0.875rem', fontWeight: 600, marginBottom: '0.25rem' }}}}>이름</label>\n"
        f"          <input type=\"text\" value={{formData.name ?? ''}} onChange={{(e) => handleChange('name', e.target.value)}} style={{{{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }}}} />\n"
        "        </div>\n"
        f"        <div>\n"
        f"          <label style={{{{ display: 'block', fontSize: '0.875rem', fontWeight: 600, marginBottom: '0.25rem' }}}}>설명</label>\n"
        f"          <textarea value={{formData.description ?? ''}} onChange={{(e) => handleChange('description', e.target.value)}} rows={{4}} style={{{{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }}}} />\n"
        "        </div>\n"
        f"        <div style={{{{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}}}>\n"
        f"          <button type=\"button\" onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>취소</button>\n"
        f"          <button type=\"submit\" disabled={{submitting}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer', opacity: submitting ? 0.5 : 1 }}}}>{{submitting ? '저장 중...' : '저장'}}</button>\n"
        "        </div>\n"
        "      </form>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def _gen_sub_page_edit(file_path: str, resource: str, label: str, parent_route: str) -> str:
    """Generate an edit form sub-page for Next.js."""
    api_path = f"/api/{resource.replace('-', '_')}"
    pascal = "".join(w.capitalize() for w in resource.replace("-", "_").split("_"))
    return (
        f"// FILE: {file_path}\n"
        f"// GENERATED BY: sub-page-generator (from href binding)\n"
        f"// PAGE TYPE: form-edit | RESOURCE: {resource}\n"
        "'use client';\n\n"
        "import React, { useState, useEffect, useCallback } from 'react';\n"
        "import { useParams, useRouter } from 'next/navigation';\n\n"
        f"export default function {pascal}EditPage() {{\n"
        "  const params = useParams();\n"
        "  const id = params?.id as string;\n"
        "  const router = useRouter();\n"
        "  const [formData, setFormData] = useState<Record<string, any>>({});\n"
        "  const [loading, setLoading] = useState(true);\n"
        "  const [submitting, setSubmitting] = useState(false);\n"
        "  const [error, setError] = useState<string | null>(null);\n\n"
        "  useEffect(() => {\n"
        "    (async () => {\n"
        "      try {\n"
        f"        const res = await fetch(`{api_path}/${{id}}`);\n"
        "        if (!res.ok) throw new Error('데이터를 불러올 수 없습니다');\n"
        "        const json = await res.json();\n"
        "        setFormData(json.data ?? json);\n"
        "      } catch (e) {\n"
        "        setError(e instanceof Error ? e.message : '오류가 발생했습니다');\n"
        "      } finally {\n"
        "        setLoading(false);\n"
        "      }\n"
        "    })();\n"
        "  }, [id]);\n\n"
        "  const handleChange = useCallback((field: string, value: any) => {\n"
        "    setFormData(prev => ({ ...prev, [field]: value }));\n"
        "  }, []);\n\n"
        "  const handleSubmit = useCallback(async (e: React.FormEvent) => {\n"
        "    e.preventDefault();\n"
        "    try {\n"
        "      setSubmitting(true);\n"
        "      setError(null);\n"
        f"      const res = await fetch(`{api_path}/${{id}}`, {{\n"
        "        method: 'PUT',\n"
        "        headers: { 'Content-Type': 'application/json' },\n"
        "        body: JSON.stringify(formData),\n"
        "      });\n"
        "      if (!res.ok) throw new Error('수정에 실패했습니다');\n"
        f"      router.push('{parent_route}');\n"
        "    } catch (e) {\n"
        "      setError(e instanceof Error ? e.message : '오류가 발생했습니다');\n"
        "    } finally {\n"
        "      setSubmitting(false);\n"
        "    }\n"
        "  }, [formData, id, router]);\n\n"
        f"  if (loading) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>로딩중...</div>;\n\n"
        "  return (\n"
        f"    <div style={{{{ display: 'flex', flexDirection: 'column', gap: '1.5rem', padding: '1rem' }}}}>\n"
        f"      <header style={{{{ display: 'flex', alignItems: 'center', gap: '1rem' }}}}>\n"
        f"        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '0.875rem', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>\n"
        f"        <h1 style={{{{ fontSize: '1.5rem', fontWeight: 800 }}}}>{label} 수정</h1>\n"
        "      </header>\n"
        "      {error && <div style={{ padding: '0.75rem', background: '#fef2f2', color: '#dc2626', borderRadius: '0.375rem' }}>{error}</div>}\n"
        f"      <form onSubmit={{handleSubmit}} style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: '1.5rem', boxShadow: '0 1px 3px rgba(0,0,0,0.1)', display: 'flex', flexDirection: 'column', gap: '1rem' }}}}>\n"
        "        {Object.entries(formData).filter(([k]) => !['id', 'created_at', 'updated_at'].includes(k)).map(([key, value]) => (\n"
        "          <div key={key}>\n"
        "            <label style={{ display: 'block', fontSize: '0.875rem', fontWeight: 600, marginBottom: '0.25rem' }}>{key}</label>\n"
        "            <input type=\"text\" value={String(value ?? '')} onChange={(e) => handleChange(key, e.target.value)} style={{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }} />\n"
        "          </div>\n"
        "        ))}\n"
        f"        <div style={{{{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}}}>\n"
        f"          <button type=\"button\" onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>취소</button>\n"
        f"          <button type=\"submit\" disabled={{submitting}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer', opacity: submitting ? 0.5 : 1 }}}}>{{submitting ? '저장 중...' : '저장'}}</button>\n"
        "        </div>\n"
        "      </form>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
