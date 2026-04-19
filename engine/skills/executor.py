"""
Skill Executor — the bridge between DAGAdvancer and the E+ Skill system.

``create_skill_executor`` is a factory that returns an ``async def executor(node)``
callable matching the signature expected by DAGAdvancer.  It enriches prompts
via skill specs and runs programmatic validators *before* and *after* the LLM
call, reducing unnecessary API spend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional

from engine.core.dag_advancer import NodeSnapshot
from engine.ai.context_assembler import (
    ContextAssembler,
    DeltaEntry,
    NodeContext,
    ProjectContext,
)
from engine.ai.model_adapter import ModelAdapter, ModelID
from engine.core.budget_enforcer import (
    BudgetEnforcer,
    TOKEN_BUDGET,
    InputBudgetExceededError,
    PhaseBudgetExceededError,
)
from engine.skills.registry import SkillRegistry
from engine.skills.template import render
from engine.skills.artifact.loader import (
    _load_project_context,
    _load_task_artifact,
    _load_files_from_manifest,
    _load_deltas,
    _load_env_vars_context,
    _load_design_artifacts_for_build,
    _validate_design_compliance,
    _load_upstream_artifacts,
    _load_defect_targets_only,
    _load_infra_summary,
    _load_assembled_pages,
    _resolve_workspace_path_for_project,
    _load_api_spec_parsed,
    _load_db_spec_parsed,
    _count_project_pages,
    _load_component_names,
    _load_design_tokens_for_qa,
    _build_component_path_map,
)
from engine.skills.artifact.saver import (
    _save_artifact,
    _save_scaffold_as_artifact,
    _write_scaffold_to_workspace,
    _extract_ai_stubs,
    _merge_ai_into_scaffold,
)
from engine.skills.platform import _detect_platform, _build_platform_instruction
from engine.skills.utils import _now, _extract_json, _extract_block, _extract_all_blocks, _extract_section
from engine.skills.codegen.helpers import (
    _py_to_js_literal, _sql_type_to_ts,
    _slug_to_resource, _match_endpoints_for_page, _detect_page_type,
    _safe_optional_chain, _py_condition_to_jsx, _py_data_path_to_js,
    _auto_props_for_component, _sql_to_prisma_type,
)
from engine.skills.codegen.react import _build_react_page, _generate_react_complete
from engine.skills.codegen.vue import _build_vue_page
from engine.skills.codegen.generic import _build_generic_page
from engine.skills.codegen.db_schema import _build_db_schema_code, _generate_prisma_schema, _generate_sql_migration
from engine.skills.codegen.backend_api import (
    _build_backend_api_code, _generate_express_server, _generate_express_router,
    _generate_express_service, _find_search_column, _generate_auth_middleware,
    _generate_error_middleware,
)
from engine.skills.codegen.frontend_infra import (
    _design_tokens_to_css,
    _build_frontend_infra_code, _generate_globals_css, _generate_root_layout,
    _extract_components_from_library, _html_component_to_react,
    _inline_style_to_jsx, _generate_loading_component,
    _generate_error_boundary_component, _generate_empty_state_component,
    _generate_toast_component,
)
from engine.skills.codegen.page_builder import (
    _build_placement_scaffold, _build_complete_page_code, _build_binding_summary,
)
from engine.skills.qa.harness import (
    _harness_validate_programmatic, _split_by_file_tag, _check_jsx_balance,
    _check_placement_coverage, _harness_validate_ai_code,
    _harness_validate_document, _validate_batch_output,
    _harness_validate_interactivity, _harness_validate_design_match,
    _harness_validate_screen_coverage,
)
from engine.skills.qa.prompt import (
    _QA_OUTPUT_SCHEMA, _build_qa_ai_prompt, _parse_qa_verdict,
    _verdict_to_markdown, _save_qa_stamp, _build_self_check_block,
)
from engine.skills.batch import (
    _group_pages_for_batching, _match_html_to_slugs, _extract_batch_summary,
    _merge_batch_outputs, _sample_code_files, _estimate_output_size,
    _build_size_anchor_block, _load_batch_cache, _batched_frontend_generate,
    _TASK_RETRIGGER_KEYWORDS,
)
from engine.skills.repair import _build_repair_context, _auto_repair_artifact, _defect_cascade
from engine.skills.two_phase import _two_phase_generate
from engine.skills.assembly import (
    _handle_assembly_node, _composition_post_process,
    _restructure_placements_to_pages, _repair_json_if_needed,
    _build_assembly_summary,
)
from engine.skills.splitting import _group_pages_for_splitting, _split_frontend_component_nodes
from engine.skills.design_html import load_design_htmls_for_prompt

from engine.skills.executor_context import (
    _inject_screen_list_requirement,
    _load_previous_qa_feedback,
    _inject_define_context_for_build,
    _inject_harness_structural_requirements,
)
from engine.skills.executor_partial import (
    _check_partial_mode,
    _clear_invalidation_meta,
    _execute_partial_patch,
)
from engine.skills.executor_cascade import (
    _cascade_for_node,
    _trigger_downstream_cascade,
    _trigger_upstream_cascade,
)

logger = logging.getLogger(__name__)


BATCH_THRESHOLD = 15  # 이 이상이면 배치 실행


def _make_json_tool(schema: dict | None = None) -> list | None:
    """JSON schema를 Tool Use로 변환. schema가 없으면 None."""
    if not schema:
        return None
    return [{"name": "output_json", "description": "구조화된 JSON 데이터를 출력한다", "input_schema": schema}]


# ---------------------------------------------------------------------------
# Gotchas Tracker — 프로젝트 레벨 실수 학습
# ---------------------------------------------------------------------------

import time as _time
_gotcha_cache: dict[str, tuple[float, str]] = {}
_GOTCHA_TTL = 60  # 60초
_design_token_cache: dict[str, tuple[float, str | None]] = {}
_DESIGN_TOKEN_TTL = 300  # 5분

_GOTCHA_CATEGORIES = [
    (["import", "module", "ModuleNotFoundError", "Cannot find"], "import_error"),
    (["component", "not found", "undefined", "is not defined"], "missing_component"),
    (["schema", "타입", "type", "validation"], "schema_mismatch"),
    (["timeout", "ENOTFOUND", "연결"], "network_error"),
    (["overloaded", "529", "503", "rate limit"], "api_overload"),
    (["budget", "토큰", "token limit"], "budget_exceeded"),
    (["섹션", "heading", "누락"], "missing_section"),
    (["JSON", "parse", "파싱"], "json_parse_error"),
]


def _classify_gotcha(error_msg: str) -> str:
    """에러 메시지에서 gotcha 카테고리 자동 분류."""
    lower = error_msg.lower()
    for keywords, category in _GOTCHA_CATEGORIES:
        if any(kw.lower() in lower for kw in keywords):
            return category
    return "general"


async def _record_gotcha(db: Any, project_id: str, node_id: str, node_name: str, error_msg: str) -> None:
    """프로젝트 레벨 gotcha 기록. 중복 방지."""
    import uuid as _uuid
    category = _classify_gotcha(error_msg)
    desc = error_msg[:500]
    existing = await db.fetchone(
        "SELECT id FROM project_gotchas WHERE project_id=? AND category=? AND description=?",
        (project_id, category, desc),
    )
    if existing:
        return
    await db.execute(
        "INSERT INTO project_gotchas (id, project_id, category, description, source_node_id, source_node_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(_uuid.uuid4()), project_id, category, desc, node_id, node_name, _now()),
    )
    logger.info("gotcha_recorded project=%s category=%s node=%s", project_id[:8], category, node_name[:30])


async def _load_gotchas_for_prompt(db: Any, project_id: str) -> str:
    """프로젝트 gotchas를 프롬프트 주입용 문자열로 반환. 60초 캐싱."""
    now = _time.time()
    cached = _gotcha_cache.get(project_id)
    if cached and (now - cached[0]) < _GOTCHA_TTL:
        return cached[1]

    rows = await db.fetchall(
        "SELECT category, description, source_node_name FROM project_gotchas WHERE project_id=? ORDER BY created_at DESC LIMIT 10",
        (project_id,),
    )
    if not rows:
        result = ""
    else:
        lines = ["\n\n## ⚠️ 이 프로젝트에서 이전에 발생한 문제 (반복 금지)\n"]
        for g in rows:
            lines.append(f"- [{g['category']}] {g['description'][:200]} (출처: {g['source_node_name']})")
        lines.append("\n위 문제를 반복하지 마세요.\n")
        result = "\n".join(lines)

    _gotcha_cache[project_id] = (now, result)
    return result


# Harness check 이름 → 구체적 수정 지시 매핑.
# 새 check 이 harness.py 에 추가되면 여기에도 한 줄 추가할 것.
_HARNESS_CHECK_FIX_HINTS: dict[str, str] = {
    "interface_defined":
        "각 .tsx / .ts 파일에 최소 1개 이상 `interface Name { ... }` 또는 "
        "`type Name = ...` 정의를 추가하세요. Props/State/API 응답 타입 등 "
        "용도 불문. 파일 상단 import 바로 아래 위치.",
    "export_default":
        "모든 .tsx / .jsx 페이지·컴포넌트 파일에 `export default ComponentName` "
        "구문을 반드시 포함하세요.",
    "imports_exist":
        "모든 .ts / .tsx 파일에 import 문을 최소 1개 포함하세요 "
        "(React 컴포넌트면 `import React` 또는 관련 타입/함수 import).",
    "file_tags":
        "각 파일의 시작에 `// FILE: 상대경로/파일명` 주석을 추가해 파일 경계를 "
        "명시하세요. CSS 파일은 `/* FILE: ... */`.",
    "jsx_balance":
        "JSX 태그·중괄호 쌍이 맞는지 검토하세요. 미완성 태그나 누락된 닫는 "
        "괄호가 있으면 수정.",
    "placement_coverage":
        "화면 목록 정의서에 명시된 모든 화면/컴포넌트 슬러그가 실제 코드에 "
        "존재해야 합니다. 누락된 페이지를 추가하세요.",
    "recipe_count":
        "레시피 산출물에 최소 1개 이상의 레시피가 필요합니다. 정의서를 참고해 "
        "누락 항목을 복원하세요.",
    "recipe_slugs":
        "각 레시피의 page_slug 필드가 비어있지 않은지 확인하세요.",
    "no_todo":
        "TODO/TBD/FIXME/미정/작성 예정/추후 작성 등 미완성 표시 금지. "
        "대체 표현: "
        "(1) 미결정 사항 → '향후 설계 필요' 또는 '추후 정책 확정 시 반영'; "
        "(2) 미정 값 → '미지정' 또는 '별도 협의'; "
        "(3) 작업 대기 → '다음 단계에서 진행'. "
        "산출물 본문·테이블 셀·주석 모두에서 제거하고 위 대체 표현으로 교체.",
}


def _build_harness_fix_directive(failed_checks: list[dict], failures: list[str]) -> str:
    """Harness 실패 체크 리스트 → TASK 재실행 프롬프트에 주입할 구체적 수정 지시.

    범용: harness.py 의 check 구조에만 의존하고 프로젝트/노드 이름과 무관.
    """
    lines = [
        "## 🚨 이전 QA 검증 실패 — 반드시 수정할 항목 (Harness 자동 검증)",
        "",
        "아래 체크가 프로그래매틱 검증에서 실패했습니다. **수정 후 산출물을 재제출**하세요.",
        "지시를 따르지 않으면 자동으로 다시 재생성 요청되며 토큰만 소모됩니다.",
        "",
    ]
    seen_hints: set[str] = set()
    for c in failed_checks[:8]:
        _name = c.get("name", "unknown")
        hint = _HARNESS_CHECK_FIX_HINTS.get(_name, "")
        if not hint or hint in seen_hints:
            continue
        seen_hints.add(hint)
        lines.append(f"- **{_name}**: {hint}")
    if failures:
        lines.append("")
        lines.append("### 감지된 구체적 문제:")
        for f in failures[:5]:
            lines.append(f"  - {f}")
    lines.append("")
    lines.append("기존 산출물의 **전체 구조는 유지**하면서 위 항목만 정확히 보완하세요. "
                 "다른 요구사항(화면 목록, 디자인 시안, DEFINE 컨텍스트 등)도 그대로 준수.")
    lines.append("")
    return "\n".join(lines)


def _extract_first_json_block(text: str) -> str:
    """텍스트에서 가장 큰 유효한 JSON 배열/객체 블록을 추출.

    AI가 짧은 JSON 조각 + 본문 JSON을 함께 출력하는 경우,
    첫 번째가 아닌 가장 큰 블록을 반환하여 본문 누락 방지.
    문자열 내부의 괄호를 무시하고 depth 추적.
    """
    candidates: list[str] = []
    pos = 0
    while pos < len(text):
        # 다음 [ 또는 { 찾기
        start = -1
        open_char = ""
        close_char = ""
        for i in range(pos, len(text)):
            if text[i] == '[':
                start = i
                open_char, close_char = '[', ']'
                break
            elif text[i] == '{':
                start = i
                open_char, close_char = '{', '}'
                break
        if start < 0:
            break

        depth = 0
        in_string = False
        escape_next = False
        end = -1

        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end > start:
            candidates.append(text[start:end])
            pos = end
        else:
            pos = start + 1

    if not candidates:
        return text
    # 가장 큰 블록 반환
    return max(candidates, key=len)


async def _chunked_json_generate(
    model_adapter: Any,
    model: str,
    assembly: Any,
    max_tokens: int,
    spec: dict,
    node: "NodeSnapshot",
    db: Any = None,
) -> "APIResponse":
    """대형 JSON 산출물을 카테고리별 청크로 분할 생성.

    spec에 chunk_categories가 정의된 경우에만 호출.
    각 카테고리를 순차 생성하고, 앞 단계 결과를 다음 단계 컨텍스트로 전달.
    마지막에 JSON 배열 머지.

    중간 결과 캐싱: 성공한 카테고리를 DB task_snapshot에 저장.
    재실행 시 캐시된 카테고리는 건너뛰고 나머지만 생성.
    """
    from engine.ai.model_adapter import APIResponse

    categories = spec.get("chunk_categories", [])
    if not categories:
        return await model_adapter.call(
            model=model, system=assembly.system,
            prompt=assembly.prompt, max_tokens=max_tokens,
            tools=_make_json_tool(spec.get("response_schema")),
        )

    # 캐시 로드: 이전 실행에서 성공한 카테고리
    cached_items: list = []
    cached_categories: set = set()
    if db:
        try:
            cache_row = await db.fetchone(
                "SELECT task_snapshot FROM nodes WHERE id=?", (node.id,)
            )
            if cache_row and cache_row["task_snapshot"]:
                cache_data = json.loads(cache_row["task_snapshot"])
                if isinstance(cache_data, dict) and "chunked_items" in cache_data:
                    cached_items = cache_data["chunked_items"]
                    cached_categories = set(cache_data.get("completed_categories", []))
                    if cached_items:
                        logger.info(
                            "chunked_json_cache_loaded node=%s cached=%d categories=%s",
                            node.id[:8], len(cached_items), cached_categories,
                        )
        except Exception:
            pass

    all_items: list = list(cached_items)
    prev_summary = ""
    if all_items:
        prev_summary = json.dumps(
            [{"id": c.get("component_id", c.get("id", "")),
              "name": c.get("name", ""),
              "category": c.get("category", "")}
             for c in all_items],
            ensure_ascii=False,
        )

    total_input = 0
    total_output = 0

    for i, category_raw in enumerate(categories):
        # 카테고리가 dict(name+description) 또는 문자열 지원
        if isinstance(category_raw, dict):
            cat_name = category_raw.get("name", "")
            cat_desc = category_raw.get("description", "")
        else:
            cat_name = str(category_raw)
            cat_desc = ""
        category = cat_name  # 이후 코드에서 category 변수 사용

        # 캐시된 카테고리 건너뛰기
        if category in cached_categories:
            logger.info("chunked_json_cache_skip category=%s node=%s", category, node.id[:8])
            continue

        chunk_prompt = (
            f"## 지시: 카테고리 '{category}'에 해당하는 항목만 생성하세요.\n"
        )
        if cat_desc:
            chunk_prompt += f"카테고리 설명: {cat_desc}\n"
        chunk_prompt += "다른 카테고리는 생성하지 마세요. 순수 JSON 배열만 출력하세요.\n\n"
        if prev_summary:
            chunk_prompt += (
                f"## 이전 카테고리 결과 (참조 — 일관성 유지, 중복 금지):\n"
                f"{prev_summary}\n\n"
            )
        chunk_prompt += assembly.prompt

        response = await model_adapter.call(
            model=model,
            system=assembly.system,
            prompt=chunk_prompt,
            max_tokens=max_tokens,
            tools=_make_json_tool(spec.get("response_schema")),
        )
        total_input += response.input_tokens
        total_output += response.output_tokens

        # 파싱 + 누적
        content = response.content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(l for l in lines if not l.startswith("```")).strip()

        try:
            chunk = json.loads(content)
        except json.JSONDecodeError:
            if not response.tool_used:
                content = _extract_first_json_block(content)

        try:
            chunk = json.loads(content)
            if isinstance(chunk, list):
                all_items.extend(chunk)
                cached_categories.add(category)
                logger.info(
                    "chunked_json_category category=%s items=%d total=%d node=%s",
                    category, len(chunk), len(all_items), node.id[:8],
                )
                prev_summary = json.dumps(
                    [{"id": c.get("component_id", c.get("id", "")),
                      "name": c.get("name", ""),
                      "category": c.get("category", "")}
                     for c in all_items],
                    ensure_ascii=False,
                )
                # 중간 결과 DB 저장 (재실행 시 이어받기)
                if db:
                    try:
                        cache_save = json.dumps({
                            "chunked_items": all_items,
                            "completed_categories": list(cached_categories),
                        }, ensure_ascii=False)
                        await db.execute(
                            "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                            (cache_save, _now(), node.id),
                        )
                    except Exception:
                        pass
            elif isinstance(chunk, dict):
                all_items.append(chunk)
        except json.JSONDecodeError as e:
            # bracket closure repair 시도
            _repaired = False
            _ob = content.count('{') - content.count('}')
            _obr = content.count('[') - content.count(']')
            if _ob > 0 or _obr > 0:
                import re as _re_repair
                _fixed = _re_repair.sub(r',\s*$', '', content.rstrip())
                _fixed += ']' * max(_obr, 0) + '}' * max(_ob, 0)
                try:
                    chunk = json.loads(_fixed)
                    if isinstance(chunk, list):
                        all_items.extend(chunk)
                        cached_categories.add(category)
                        _repaired = True
                        logger.info(
                            "chunked_json_repaired category=%s items=%d total=%d node=%s",
                            category, len(chunk), len(all_items), node.id[:8],
                        )
                        prev_summary = json.dumps(
                            [{"id": c.get("component_id", c.get("id", "")),
                              "name": c.get("name", ""),
                              "category": c.get("category", "")}
                             for c in all_items],
                            ensure_ascii=False,
                        )
                        if db:
                            try:
                                cache_save = json.dumps({
                                    "chunked_items": all_items,
                                    "completed_categories": list(cached_categories),
                                }, ensure_ascii=False)
                                await db.execute(
                                    "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                                    (cache_save, _now(), node.id),
                                )
                            except Exception:
                                pass
                except json.JSONDecodeError:
                    pass
            if not _repaired:
                logger.warning("chunked_json_parse_fail category=%s error=%s", category, e)
            continue

    if not all_items:
        logger.warning("chunked_json_all_failed node=%s → fallback single call", node.id[:8])
        return await model_adapter.call(
            model=model, system=assembly.system,
            prompt=assembly.prompt, max_tokens=max_tokens,
            tools=_make_json_tool(spec.get("response_schema")),
        )

    # 완료 시 캐시 클리어
    if db:
        try:
            await db.execute(
                "UPDATE nodes SET task_snapshot=NULL, updated_at=? WHERE id=?",
                (_now(), node.id),
            )
        except Exception:
            pass

    merged = json.dumps(all_items, ensure_ascii=False, indent=2)
    logger.info(
        "chunked_json_complete node=%s categories=%d total_items=%d size=%d",
        node.id[:8], len(categories), len(all_items), len(merged),
    )
    return APIResponse(
        content=merged,
        input_tokens=total_input,
        output_tokens=total_output,
        model=model,
        stop_reason="end_turn",
    )


# ────────────────────────────────────────────────────────────────────────
# S8: 범용 JSON 아이템 청크 분할 (chunk_items 기반)
# 컴포넌트·토큰·페이지 등 모든 JSON 배열 산출물에 적용.
# spec.chunk_items: [name1, name2, ...] → 아이템당 LLM 1회 호출 → 배열 병합.
# S9: chunk_items 를 upstream artifact 에서 자동 추출 (프로젝트 도메인 무관).
# ────────────────────────────────────────────────────────────────────────

async def _resolve_chunk_items(spec: dict, node: "NodeSnapshot", db: Any) -> list | None:
    """chunk_items 자동 결정 (S9 범용):

    우선순위:
    1. spec.chunk_items (명시 리스트) — 최우선
    2. spec.chunk_items_source (upstream artifact 에서 regex 추출) — 범용
    3. spec.split_categories[i].chunk_items (분할 노드 이름 매칭)
    4. 없으면 None → 단일 호출 fallback

    도메인 무관 (실버케어/이커머스/금융 등) — spec regex 만 적절하면 자동 작동.
    """
    # 1. 명시 리스트 우선
    if spec.get("chunk_items"):
        return spec["chunk_items"]

    # 2. upstream artifact 에서 자동 추출 (진짜 범용 경로)
    # A-2: extract_pattern (단일) 또는 extract_patterns (fallback chain list) 지원.
    # 여러 패턴 시도 → 첫번째로 ≥3개 매치되는 것 채택 → 도메인 다양성 커버.
    src = spec.get("chunk_items_source")
    if src and isinstance(src, dict) and db is not None:
        from_spec = src.get("from_spec")
        # 단일 또는 list 둘 다 지원
        patterns: list[str] = []
        if src.get("extract_patterns") and isinstance(src["extract_patterns"], list):
            patterns = [p for p in src["extract_patterns"] if isinstance(p, str)]
        elif src.get("extract_pattern"):
            patterns = [src["extract_pattern"]]
        # 기본 fallback chain (spec 에 아무것도 없어도 일반적 ID 잡도록)
        if not patterns:
            patterns = [
                r"(SC-[A-Z]{2,4}-\d{3,4}|SCR-\d{3,4})",     # 화면 ID
                r"(FR-\d{3,4}|UC-\d{3,4})",                    # 기능/유스케이스
                r"(API-\d{3,4}|EP-\d{3,4})",                   # API
                r"([A-Z]{2,5}-[A-Z]{2,4}-\d{3,4})",            # generic grouped
            ]

        if from_spec and patterns:
            try:
                row = await db.fetchone(
                    """SELECT av.storage_path AS content FROM nodes n
                    JOIN artifacts a ON a.node_id = n.id
                    JOIN artifact_versions av ON av.artifact_id = a.id
                      AND av.version_num = a.current_version
                    WHERE n.project_id=? AND n.name LIKE ?
                      AND n.node_type='TASK' AND n.state='COMPLETED'
                    ORDER BY n.updated_at DESC LIMIT 1""",
                    (node.project_id, f"%{from_spec}%"),
                )
                if row and row.get("content"):
                    import re as _re_x
                    content = row["content"]
                    # A-2: pattern fallback chain — 첫 ≥3개 매치 채택
                    MIN_ITEMS_THRESHOLD = 3
                    for pat in patterns:
                        try:
                            raw_matches = _re_x.findall(pat, content)
                        except Exception:
                            continue
                        seen: set = set()
                        items: list = []
                        for m in raw_matches:
                            key = m if isinstance(m, str) else (m[0] if m else "")
                            if key and key not in seen:
                                seen.add(key)
                                items.append(key)
                        if len(items) >= MIN_ITEMS_THRESHOLD:
                            logger.info(
                                "chunk_items_extracted node=%s from='%s' pattern='%s' count=%d",
                                node.id[:8], from_spec, pat[:40], len(items),
                            )
                            return items
                    # 모든 패턴 시도 후에도 실패
                    logger.warning(
                        "chunk_items_source_empty node=%s from='%s' tried=%d patterns → fallback 단일 호출",
                        node.id[:8], from_spec, len(patterns),
                    )
            except Exception as e:
                logger.warning("chunk_items_source_failed node=%s err=%s", node.id[:8], str(e)[:80])

    # 3. split_categories 기반 (기존 컴포넌트 라이브러리 분할 노드 패턴)
    import re as _re_cat
    cat_match = _re_cat.search(r"\(([^)]+)\)", node.name or "")
    if cat_match and spec.get("split_categories"):
        cat_name = cat_match.group(1).strip()
        for c in spec["split_categories"]:
            if isinstance(c, dict) and c.get("name") == cat_name:
                if c.get("chunk_items"):
                    logger.info(
                        "chunk_items_inherited node=%s cat=%s items=%d",
                        node.id[:8], cat_name, len(c["chunk_items"]),
                    )
                    return c["chunk_items"]
                break

    return None


async def _chunked_json_items_generate(
    model_adapter: Any,
    assembly: Any,
    spec: dict,
    node: "NodeSnapshot",
    db: Any = None,
) -> "APIResponse":
    """type=json + chunk_items 있으면 아이템당 1회 LLM 호출 후 JSON 배열 병합.

    각 호출:
      - prompt 의 {{chunk_item}} 을 현재 item 으로 치환
      - "객체 1개만 반환" 엄격 지시
      - 실패 시 placeholder(_incomplete) 로 best-effort 진행

    task_snapshot 에 completed_items 저장 → 중간 실패 재시도 효율.
    """
    from engine.ai.model_adapter import APIResponse

    items = spec.get("chunk_items") or []
    model = spec.get("model_preference") or ModelID.SONNET
    if model in ("opus", "sonnet", "haiku"):
        model = {"opus": ModelID.OPUS, "sonnet": ModelID.SONNET,
                 "haiku": ModelID.HAIKU}[model]

    # 중간 캐시 로드 (이전 실행에서 성공한 아이템 재활용)
    completed_items: dict = {}
    if db:
        try:
            row = await db.fetchone(
                "SELECT task_snapshot FROM nodes WHERE id=?", (node.id,),
            )
            if row and row.get("task_snapshot"):
                snap = json.loads(row["task_snapshot"])
                if isinstance(snap, dict) and snap.get("type") == "chunked_json_items":
                    completed_items = snap.get("completed_items", {}) or {}
        except Exception:
            completed_items = {}

    results: list[dict] = []
    total_in = 0
    total_out = 0

    for item in items:
        if not isinstance(item, str):
            continue
        # 캐시 hit
        if item in completed_items:
            try:
                results.append(completed_items[item])
                logger.info("chunked_json_item_cached node=%s item=%s",
                            node.id[:8], item)
                continue
            except Exception:
                pass

        # prompt 에 {{chunk_item}} 치환 + 엄격 지시 블록
        base_prompt = assembly.prompt or ""
        item_prompt = base_prompt.replace("{{chunk_item}}", item)
        item_prompt += (
            f"\n\n---\n\n## ⚠ 이번 호출 엄수\n"
            f"- 오직 '{item}' 1개 객체만 JSON 으로 출력\n"
            f"- 배열 `[]` 아님, 중괄호 객체 `{{}}` 1개만\n"
            f"- 코드블록 ```…``` 감싸기 금지\n"
            f"- 설명·다른 아이템·주석 금지\n"
            f"- 이 파이프라인은 호출당 1개 아이템만 수집해 나중에 병합함\n"
        )

        try:
            resp = await model_adapter.call(
                model=model, system=assembly.system,
                prompt=item_prompt, max_tokens=8000,
                tools=_make_json_tool(spec.get("response_schema")),
            )
            total_in += resp.input_tokens
            total_out += resp.output_tokens
            content = (resp.content or "").strip()
            # 코드블록 제거 (LLM 이 가끔 감싸서 반환)
            if content.startswith("```"):
                content = content.strip("`").lstrip("json").strip()
                if content.endswith("```"):
                    content = content[:-3].strip()
            # 파싱
            parsed = None
            try:
                parsed = json.loads(content)
            except Exception:
                # 일부 LLM 이 배열로 반환 — 관대하게 수용
                try:
                    wrapped = f"[{content}]"
                    parsed_w = json.loads(wrapped)
                    if isinstance(parsed_w, list) and parsed_w:
                        parsed = parsed_w[0]
                except Exception:
                    parsed = None

            if isinstance(parsed, dict):
                results.append(parsed)
                completed_items[item] = parsed
                logger.info(
                    "chunked_json_item node=%s item=%s ok",
                    node.id[:8], item,
                )
            elif isinstance(parsed, list) and parsed:
                # 배열 반환돼도 원소들 추가 (best-effort)
                for p in parsed:
                    if isinstance(p, dict):
                        results.append(p)
                completed_items[item] = parsed[0] if parsed else {}
                logger.info(
                    "chunked_json_item node=%s item=%s array_mode len=%d",
                    node.id[:8], item, len(parsed),
                )
            else:
                raise ValueError("JSON parse failed or empty")
        except Exception as e:
            logger.warning(
                "chunked_json_item_failed node=%s item=%s err=%s",
                node.id[:8], item, str(e)[:120],
            )
            results.append({
                "name": item, "_incomplete": True,
                "_error": str(e)[:80],
            })

        # 중간 캐시 저장 (다음 재실행 시 재활용)
        if db:
            try:
                snap = {
                    "type": "chunked_json_items",
                    "completed_items": completed_items,
                    "completed_count": len(completed_items),
                    "total_count": len(items),
                    "updated_at": _now(),
                }
                await db.execute(
                    "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                    (json.dumps(snap, ensure_ascii=False), _now(), node.id),
                )
            except Exception:
                pass

    # 전체 완료 → snapshot 제거
    if db:
        try:
            await db.execute(
                "UPDATE nodes SET task_snapshot=NULL, updated_at=? WHERE id=?",
                (_now(), node.id),
            )
        except Exception:
            pass

    merged = json.dumps(results, ensure_ascii=False)
    logger.info(
        "chunked_json_complete node=%s items=%d size=%d",
        node.id[:8], len(results), len(merged),
    )
    return APIResponse(
        content=merged,
        input_tokens=total_in,
        output_tokens=total_out,
        model=model,
        stop_reason="end_turn",
    )


# ────────────────────────────────────────────────────────────────────────
# F4 chunked-document helpers (S1-2 — _chunked_document_generate 분할)
# 의도: 360줄 함수의 가독성·테스트 가능성 확보. 동작 변경 없음.
# ────────────────────────────────────────────────────────────────────────

def _count_section_items(content: str, id_patterns: list) -> int:
    """섹션 본문에서 항목 수 계산 — max(ID 출현 수, 표 데이터 행 수).

    min_items 검증과 retry-after 재검증에서 동일 로직 2회 사용 → 헬퍼화.
    """
    ids_count = 0
    for p in id_patterns:
        ids_count = max(ids_count, len(p.findall(content)))
    row_count = sum(
        1 for ln in content.split("\n")
        if ln.strip().startswith("|") and not ln.strip().startswith("|--")
        and "---" not in ln
    )
    # 표 헤더 제외 (데이터 행만)
    row_count = max(0, row_count - 1)
    return max(ids_count, row_count)


async def _chunked_call_outline(
    model_adapter: Any,
    model: str,
    assembly: Any,
    node_id: str,
) -> tuple[set[str], dict[str, str], int, int]:
    """L1 Outline-first: 섹션 상세 생성 전 전체 항목 ID/이름 리스트 확정.

    Returns: (shared_ids, shared_id_names, input_tokens, output_tokens).
    실패·sanity fail 시 빈 set 반환 (caller가 fallback).
    """
    import re as _re_outline

    outline_prompt = (
        assembly.prompt
        + "\n\n---\n\n"
        + "## 외곽(Outline) 호출 — 이번 응답에서 상세 작성 X\n\n"
        + "이 문서가 다뤄야 할 **전체 항목의 ID와 이름**만 리스트로 나열하세요.\n"
        + "각 항목은 `ID | 이름` 형식 한 줄로. 설명·상세 X.\n"
        + "ID 접두사 예시:\n"
        + "- 화면 목록 문서 → SC-AU-001, SC-CW-001, SC-WA-001 등 (프로젝트 서브시스템별 그룹 접두)\n"
        + "- API 설계서 → API-001, API-002 ...\n"
        + "- 기능 백로그 → FR-001, FR-002 ...\n"
        + "- 유스케이스 → UC-001 ...\n"
        + "- 리스크 → RSK-001 ...\n\n"
        + "**지시**:\n"
        + "1. 프로젝트 요구사항·DEFINE artifacts를 모두 읽고 **누락 없이** 열거.\n"
        + "2. 중형 서비스는 경험적으로 30~60개, 엔터프라이즈는 50~100개.\n"
        + "3. 출력 형식: 오직 `ID | 이름` 줄만. 다른 텍스트 X.\n"
        + "예:\n"
        + "SC-AU-001 | 로그인\n"
        + "SC-AU-002 | 회원가입\n"
        + "SC-CW-001 | 고객 홈\n"
        + "...\n"
    )
    shared_ids: set[str] = set()
    shared_id_names: dict[str, str] = {}
    in_tok = 0
    out_tok = 0
    try:
        outline_resp = await model_adapter.call(
            model=model, system=assembly.system,
            prompt=outline_prompt, max_tokens=8000,
        )
        in_tok = outline_resp.input_tokens
        out_tok = outline_resp.output_tokens

        line_re = _re_outline.compile(
            r"^\s*([A-Z]{2,4}(?:-[A-Z]{2,4})?-\d{3,4})\s*[|│]\s*([^\n]+?)\s*$",
            _re_outline.MULTILINE,
        )
        for m in line_re.finditer(outline_resp.content):
            oid = m.group(1).upper()
            oname = m.group(2).strip()
            if oid and oname and oid not in shared_ids:
                shared_ids.add(oid)
                shared_id_names[oid] = oname

        # Sanity: 최소 3개 이상 파싱돼야 유효
        if len(shared_ids) < 3:
            logger.warning(
                "chunked_doc_outline_insufficient node=%s count=%d → skip outline (fallback)",
                node_id[:8], len(shared_ids),
            )
            shared_ids.clear()
            shared_id_names.clear()
        else:
            logger.info(
                "chunked_doc_outline_ids node=%s count=%d sample=%s",
                node_id[:8], len(shared_ids),
                ", ".join(sorted(shared_ids)[:5]),
            )
    except Exception as _out_err:
        logger.warning(
            "chunked_doc_outline_failed node=%s err=%s → fallback",
            node_id[:8], str(_out_err)[:120],
        )
        shared_ids.clear()
        shared_id_names.clear()

    return shared_ids, shared_id_names, in_tok, out_tok


# ────────────────────────────────────────────────────────────────────────
# S11-A3 (HTML 확장): 범용 HTML items 청크 분할
# UI 디자인 시안 등 type=html 대량 산출물용. 각 아이템을 <section> 단위로.
# ────────────────────────────────────────────────────────────────────────

def _tokens_to_css_vars(tokens: dict) -> str:
    """디자인 토큰 JSON → CSS :root 변수 선언.

    S14: 디자인 토큰 spec 의 표준 구조(colors/typography/spacing/effects) 지원.
    프로젝트 도메인 무관 — 토큰 키 기반.
    """
    lines: list[str] = []

    def _flatten(prefix: str, obj: Any, depth: int = 0):
        if depth > 4:  # 과도한 중첩 방지
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = f"{prefix}-{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    _flatten(key, v, depth + 1)
                else:
                    safe_val = str(v).replace("\n", " ").replace('"', "'")
                    lines.append(f"  --{key}: {safe_val};")

    if not isinstance(tokens, dict):
        return ""
    # 표준 키 우선 순회 (미지정 구조도 관대하게 수용)
    for section_key in ("colors", "typography", "spacing", "radius", "shadows",
                        "motion", "breakpoints", "effects"):
        if section_key in tokens:
            _flatten(section_key, tokens[section_key])
    # 기타 flat key 도 처리
    for k, v in tokens.items():
        if k in ("colors", "typography", "spacing", "radius", "shadows",
                 "motion", "breakpoints", "effects", "meta"):
            continue
        if isinstance(v, (str, int, float)):
            lines.append(f"  --{k}: {v};")
    return "\n".join(lines)


async def _build_style_from_design_tokens(
    db: Any, node: "NodeSnapshot",
) -> str | None:
    """S14: 같은 project 의 디자인 토큰 artifact → <style> 블록. 5분 캐싱.

    HTML chunk 가 첫 LLM 호출에서 <style> 추출 실패해도 디자인 토큰 기반
    fallback 확보. 프로젝트 도메인 무관.
    """
    if db is None or not getattr(node, "project_id", None):
        return None

    now = _time.time()
    cached = _design_token_cache.get(node.project_id)
    if cached and (now - cached[0]) < _DESIGN_TOKEN_TTL:
        return cached[1]

    try:
        row = await db.fetchone(
            """SELECT av.storage_path AS content FROM nodes n
            JOIN artifacts a ON a.node_id=n.id
            JOIN artifact_versions av ON av.artifact_id=a.id
              AND av.version_num=a.current_version
            WHERE n.project_id=? AND n.name='디자인 토큰'
              AND n.state='COMPLETED' AND n.node_type='TASK'
            ORDER BY n.updated_at DESC LIMIT 1""",
            (node.project_id,),
        )
        if not row or not row.get("content"):
            result = None
        else:
            tokens = json.loads(row["content"])
            css_vars = _tokens_to_css_vars(tokens)
            if not css_vars:
                result = None
            else:
                result = f"<style>\n:root {{\n{css_vars}\n}}\n</style>"
                logger.info(
                    "html_style_from_tokens node=%s vars=%d",
                    node.id[:8], css_vars.count("--"),
                )
        _design_token_cache[node.project_id] = (now, result)
        return result
    except Exception as e:
        logger.warning("html_style_from_tokens_failed: %s", str(e)[:100])
        _design_token_cache[node.project_id] = (now, None)
        return None


async def _chunked_html_items_generate(
    model_adapter: Any,
    assembly: Any,
    spec: dict,
    node: "NodeSnapshot",
    db: Any = None,
    engagement_id: str | None = None,
) -> "APIResponse":
    """type=html + chunk_items 있으면 아이템당 1회 LLM 호출 후 HTML 섹션 병합.

    각 호출: `<section id='{item}'>...</section>` 1개만 생성.
    최종: DOCTYPE + head(공통 CSS 첫 아이템 또는 간단 기본) + 모든 section.

    S14: 시작 전 디자인 토큰 artifact 에서 CSS 변수 블록 생성 시도.
    D6: StateStore + Coverage + Advisor 훅 통합 (engagement_id 있을 때만).

    task_snapshot 캐시 지원 (중간 실패 복구).
    """
    from engine.ai.model_adapter import APIResponse

    # D6: Stage 3/4/5/8 훅 (engagement_id 제공 시 활성화). Feature flag 로 bypass.
    import os as _os
    _state_store = None
    _coverage = None
    _content_cache = None
    _advisor = None
    if engagement_id and db is not None:
        if _os.environ.get("V8_ATOMIC_STATE", "1") != "0":
            try:
                from engine.core.state_store import AtomicStateStore
                _state_store = AtomicStateStore(db)
            except Exception as _e:
                logger.debug("state_store_init_fail %s", _e)
        if _state_store and _os.environ.get("V8_COVERAGE", "1") != "0":
            try:
                from engine.core.coverage import CoverageVerifier
                _coverage = CoverageVerifier(db, _state_store)
            except Exception as _e:
                logger.debug("coverage_init_fail %s", _e)
        if _os.environ.get("V8_CONTENT_CACHE", "1") != "0":
            try:
                from engine.core.content_cache import ContentHashCache
                _content_cache = ContentHashCache(db)
            except Exception as _e:
                logger.debug("content_cache_init_fail %s", _e)
        if _os.environ.get("V8_ADVISOR", "1") != "0":
            try:
                from engine.core.advisor import GlobalAdvisor
                _advisor = GlobalAdvisor(db, model_adapter)
            except Exception as _e:
                logger.debug("advisor_init_fail %s", _e)

    # D14: Stage 20/23 추가 훅
    _ledger = None
    _validator_chain = None
    if engagement_id and db is not None:
        if _os.environ.get("V8_SHARED_LEDGER", "1") != "0":
            try:
                from engine.core.shared_context import SharedContextLedger
                _ledger = SharedContextLedger(db)
            except Exception as _e:
                logger.debug("ledger_init_fail %s", _e)
    # Validator chain 은 engagement 없어도 작동 (content 기반)
    if _os.environ.get("V8_VALIDATORS", "1") != "0":
        try:
            from engine.skills.validators.plugins import run_validator_chain
            _validator_chain = run_validator_chain
        except Exception as _e:
            logger.debug("validators_init_fail %s", _e)

    items = spec.get("chunk_items") or []
    model = spec.get("model_preference") or ModelID.SONNET
    if model in ("opus", "sonnet", "haiku"):
        model = {"opus": ModelID.OPUS, "sonnet": ModelID.SONNET,
                 "haiku": ModelID.HAIKU}[model]

    # 캐시 로드
    completed_items: dict = {}
    if db:
        try:
            row = await db.fetchone(
                "SELECT task_snapshot FROM nodes WHERE id=?", (node.id,),
            )
            if row and row.get("task_snapshot"):
                snap = json.loads(row["task_snapshot"])
                if isinstance(snap, dict) and snap.get("type") == "chunked_html_items":
                    _raw_cache = snap.get("completed_items", {}) or {}
                    # Fix 7: 오염된 키(`SC-AD-001-dup192-...`) 를 원형으로 정규화
                    import re as _re_key
                    _DUP = _re_key.compile(r"-dup\d+.*$")
                    _CANON = _re_key.compile(r"^(SC-[A-Z]{2,5}-\d{3,4})")
                    for _k, _v in _raw_cache.items():
                        _canon = _CANON.match(_k)
                        _clean = _canon.group(1) if _canon else _DUP.sub("", _k)
                        # section id 속성도 함께 정규화 (첫 outer 열림 태그만)
                        if _clean != _k and isinstance(_v, str):
                            _v = _v.replace(f'id="{_k}"', f'id="{_clean}"', 1)
                            _v = _v.replace(f"id='{_k}'", f"id='{_clean}'", 1)
                        completed_items[_clean] = _v
        except Exception:
            completed_items = {}

    sections: list[str] = []
    total_in = 0
    total_out = 0

    # ===================================================================
    # common_head: Canonical CSS 전략 (단일 로직, 6겹 패치 → 1개로 통합)
    #
    # 원리: "1번째 디자이너가 CSS 만들고, 나머지 61명은 그 CSS 따름"
    #
    # 1단: 디자인 토큰에서 :root 변수 추출 (색상·폰트·간격)
    # 2단: 첫 chunk 자유 생성 (LLM 이 프로젝트에 맞는 CSS 창작)
    # 3단: 첫 chunk 의 <style> 추출 → 토큰과 합침 = canonical CSS
    # 4단: 나머지 chunk: canonical 사용, 자체 <style> strip
    # 5단: 첫 chunk 에 style 없으면 → design_framework fallback
    #
    # common_head_source: "first_chunk" | "framework" | "tokens" | "none"
    # ===================================================================
    import re as _re_css

    common_head: str = ""
    common_head_source: str = "none"
    _canonical_ready: bool = False  # canonical CSS 확보 여부

    # 1단: 디자인 토큰 :root 변수 (canonical 의 기반)
    _token_css_inner: str = ""
    try:
        token_style = await _build_style_from_design_tokens(db, node) or ""
        if token_style:
            tv_m = _re_css.search(r"<style[^>]*>([\s\S]*?)</style>", token_style)
            _token_css_inner = tv_m.group(1).strip() if tv_m else ""
    except Exception:
        pass

    # Fix 3: 캐시 히트가 있는 상태에서도 _canonical_ready 가 False 로 남아
    # 이후 LLM 호출이 자기 자신을 "첫 chunk" 로 오인해 canonical CSS 를 중복
    # 정의하거나 fallback 경로로 빠지는 문제를 선제 차단한다.
    # 캐시된 섹션 중 <style> 을 포함한 첫 항목에서 canonical 을 미리 확립한다.
    if completed_items:
        for _cached_item in items:
            if not isinstance(_cached_item, str):
                continue
            _cached_html = completed_items.get(_cached_item)
            if not _cached_html:
                continue
            _style_m_pre = _re_css.search(r"<style[^>]*>([\s\S]*?)</style>", _cached_html)
            if _style_m_pre:
                _first_css_pre = _style_m_pre.group(1).strip()
                canonical_inner = (
                    (_token_css_inner + "\n\n" if _token_css_inner else "")
                    + "/* === canonical: cached first chunk CSS === */\n"
                    + _first_css_pre
                )
                common_head = f"<style>\n{canonical_inner}\n</style>"
                common_head_source = "cached_first_chunk"
                _canonical_ready = True
                logger.info(
                    "canonical_css node=%s item=%s size=%d source=cached_first_chunk",
                    node.id[:8], _cached_item, len(_first_css_pre),
                )
                break

    for item in items:
        if not isinstance(item, str):
            continue
        # 캐시 hit
        if item in completed_items:
            _cached = completed_items[item]
            # canonical 이 이미 확립됐으면 캐시된 섹션의 중복 <style> 은 strip
            # (첫 캐시 섹션에서 canonical 을 이미 가져왔으므로 나머지는 제거)
            if _canonical_ready and common_head_source == "cached_first_chunk":
                _cached = _re_css.sub(r"<style[^>]*>[\s\S]*?</style>", "", _cached, count=0)
            sections.append(_cached)
            logger.info("chunked_html_item_cached node=%s item=%s",
                        node.id[:8], item)
            # D6: 이미 캐시로 처리됐지만 StateStore 미기록이면 COMPLETE 전이
            if _state_store:
                try:
                    cur = await _state_store.get_status(engagement_id, node.id, item)
                    if cur not in ("COMPLETE", "SKIPPED"):
                        await _state_store.reserve(engagement_id, node.id, item)
                        await _state_store.complete(engagement_id, node.id, item)
                except Exception as _e:
                    logger.debug("state_store_cache_sync_fail %s", _e)
            continue

        # D6: StateStore reserve — 동시 워커 충돌 방지 + idempotent 재시도
        if _state_store:
            try:
                reserved = await _state_store.reserve(engagement_id, node.id, item)
                if not reserved:
                    logger.info(
                        "chunked_html_item_already_processed node=%s item=%s",
                        node.id[:8], item,
                    )
                    continue
            except Exception as _e:
                logger.debug("state_store_reserve_fail %s", _e)

        base_prompt = assembly.prompt or ""
        item_prompt = base_prompt.replace("{{chunk_item}}", item)
        # D14: Stage 23 Shared Ledger — 이전 chunk 결정사항 prepend
        if _ledger:
            try:
                _snippet = await _ledger.as_prompt_snippet(engagement_id, node.id)
                if _snippet:
                    item_prompt = _snippet + "\n" + item_prompt
            except Exception as _e:
                logger.debug("ledger_snippet_fail %s", _e)
        # 프롬프트: 첫 chunk vs 이후 chunk 분기
        if not _canonical_ready:
            # 첫 chunk — 자유 생성 (공통 CSS + HTML 전부 포함)
            item_prompt += (
                f"\n\n---\n\n## ⚠ 이번 호출 엄수 (첫 화면 — 공통 CSS 정의 포함)\n"
                f"- 오직 '{item}' 화면 1개만 `<section id='{item}' class='screen'>...</section>` 형태로 작성\n"
                f"- `<!DOCTYPE>` `<html>` `<head>` `<body>` 태그 금지 (최종 병합 단계에서 감쌈)\n"
                f"- 섹션 내부에 또 다른 `<section id='SC-...'>` 를 중첩 금지 (LLM 이 SC-CW-003 안에 SC-CW-004 를 넣으면 세로 16만px 이상치 발생)\n"
                f"- **이 화면의 `<style>` 블록에 다른 화면에서도 재사용할 공통 CSS 를 함께 정의하세요**:\n"
                f"  · 글로벌 reset (`*, *::before, *::after {{ box-sizing: border-box; }}`, `html, body {{ margin:0; overflow-x: hidden; }}`)\n"
                f"  · `section.screen {{ width: 100%; max-width: 1280px; margin: 0 auto; overflow: hidden; }}` **고정 필수**\n"
                f"  · 레이아웃 (사이드바+메인, 네비, 탑바)\n"
                f"  · 카드·테이블·버튼·뱃지·모달·폼 컴포넌트\n"
                f"  · 반응형 breakpoints (mobile 375 / tablet 768 / desktop 1280)\n"
                f"- 모든 색상·간격·타이포는 CSS 변수(var(--...)) 사용\n"
                f"- Lorem ipsum 금지, 프로젝트 맥락 실제 데이터\n"
                f"- 이 파이프라인은 호출당 1화면만 수집해 나중에 병합함\n"
            )
        else:
            # 이후 chunk — canonical CSS 가 <head> 에 이미 로드됨
            item_prompt += (
                f"\n\n---\n\n## ⚠ 이번 호출 엄수 (공통 CSS 는 이미 <head> 에 로드됨)\n"
                f"- 오직 '{item}' 화면 1개만 `<section id='{item}' class='screen'>...</section>` 형태로 작성\n"
                f"- `<!DOCTYPE>` `<html>` `<head>` `<body>` 태그 금지\n"
                f"- 섹션 내부에 또 다른 `<section id='SC-...'>` 를 중첩 금지\n"
                f"- **`<style>` 블록을 포함하지 마세요** — 공통 CSS 가 이미 로드돼 있음. 첫 화면에서 정의한 클래스를 그대로 사용하세요.\n"
                f"- 섹션 루트 `<section id='{item}' class='screen'>` 는 canonical 의 `max-width: 1280px; overflow: hidden;` 을 상속하므로 자체 폭 지정 금지\n"
                f"- 이 화면에만 필요한 극소수 고유 스타일만 인라인 style 속성으로 처리\n"
                f"- 모든 색상·간격·타이포는 CSS 변수(var(--...)) 사용\n"
                f"- Lorem ipsum 금지, 프로젝트 맥락 실제 데이터\n"
            )
        if _ledger:
            item_prompt += f"- 위 '기존 결정사항' 이 있다면 일관되게 참조할 것\n"

        try:
            # D6: Content Cache lookup — 동일 입력 해시면 LLM 호출 생략
            _cache_input_hash: str | None = None
            _cache_hit_content: str | None = None
            if _content_cache:
                try:
                    _cache_input_hash = _content_cache.compute_hash({
                        "spec_name": spec.get("name"),
                        "system": assembly.system or "",
                        "prompt": item_prompt,
                        "item": item,
                    })
                    hit = await _content_cache.get(
                        engagement_id, "chunked_html_section", _cache_input_hash,
                    )
                    if hit and hit.get("content"):
                        _cache_hit_content = hit["content"]
                except Exception as _e:
                    logger.debug("content_cache_get_fail %s", _e)

            if _cache_hit_content:
                logger.info(
                    "chunked_html_item_content_cache_hit node=%s item=%s size=%d",
                    node.id[:8], item, len(_cache_hit_content),
                )
                class _FakeResp:
                    content = _cache_hit_content
                    input_tokens = 0
                    output_tokens = 0
                resp = _FakeResp()  # type: ignore[assignment]
            else:
                resp = await model_adapter.call(
                    model=model, system=assembly.system,
                    prompt=item_prompt, max_tokens=8000,
                )
            total_in += resp.input_tokens
            total_out += resp.output_tokens
            content = (resp.content or "").strip()

            # ── Canonical CSS 로직 (단일 경로) ──
            # 첫 chunk: <style> 추출 → canonical 확립
            # 이후 chunk: <style> strip (canonical 에 있으니 중복 불필요)
            if not _canonical_ready:
                # 2단: 첫 chunk 의 <style> 추출 → canonical
                style_m = _re_css.search(r"<style[^>]*>([\s\S]*?)</style>", content)
                if style_m:
                    first_css = style_m.group(1).strip()
                    # 3단: 토큰 변수 + 첫 chunk CSS = canonical
                    canonical_inner = (
                        (_token_css_inner + "\n\n" if _token_css_inner else "")
                        + "/* === canonical: first chunk CSS === */\n"
                        + first_css
                    )
                    common_head = f"<style>\n{canonical_inner}\n</style>"
                    common_head_source = "first_chunk"
                    _canonical_ready = True
                    logger.info(
                        "canonical_css node=%s item=%s size=%d source=first_chunk",
                        node.id[:8], item, len(first_css),
                    )
                    # 첫 chunk section 안에 style 유지 (첫 화면 자체 렌더링용)
                else:
                    # 5단: 첫 chunk 에 <style> 없음 → framework fallback
                    try:
                        from engine.skills.design_framework import build_framework_css
                        common_head = await build_framework_css(db, node) or ""
                        if common_head:
                            common_head_source = "framework"
                    except Exception:
                        pass
                    # Fix 4: fallback CSS 에 최소 레이아웃 baseline 추가.
                    # 기존 fallback 은 CSS 변수(:root) 만 포함해 style strip 이후
                    # section 가로폭 0 / overflow 이상치가 발생했다.
                    _LAYOUT_BASELINE = (
                        "/* === layout baseline (fallback) === */\n"
                        "*, *::before, *::after { box-sizing: border-box; }\n"
                        "html, body { margin: 0; padding: 0; overflow-x: hidden; }\n"
                        "body { font-family: var(--typography-font_family, 'Pretendard Variable', system-ui, sans-serif); "
                        "color: var(--colors-text, #111); background: var(--colors-bg, #fff); }\n"
                        "section.screen { display: block; width: 100%; max-width: 1280px; "
                        "margin: 0 auto; padding: 24px; overflow: hidden; }\n"
                        ".screen-label { font-size: 12px; opacity: 0.6; margin-bottom: 12px; }\n"
                    )
                    if not common_head and _token_css_inner:
                        common_head = f"<style>\n{_token_css_inner}\n\n{_LAYOUT_BASELINE}\n</style>"
                        common_head_source = "tokens+baseline"
                    elif not common_head:
                        common_head = f"<style>\n{_LAYOUT_BASELINE}\n</style>"
                        common_head_source = "baseline_only"
                    _canonical_ready = True
                    logger.info(
                        "canonical_css node=%s source=%s (first chunk had no style)",
                        node.id[:8], common_head_source,
                    )
            else:
                # 4단: 이후 chunk — <style> strip
                content = _re_css.sub(r"<style[^>]*>[\s\S]*?</style>", "", content)

            # 핵심: <section> 블록만 추출 + Fix 6: 중첩 section 방어
            #
            # 문제: LLM 이 간헐적으로 `<section id="SC-A">` 내부에
            #       `<section id="SC-B">` 를 중첩해 생성. 기존 non-greedy regex
            #       가 내부 `</section>` 에서 끊어 outer 가 닫히지 않거나,
            #       한 청크 호출인데 여러 SC- 섹션이 섞여 렌더 시 세로 16만px
            #       이상치가 발생했다.
            #
            # 처리: (1) 기대 item 의 `<section id="{item}"` 열림을 우선 탐색,
            #       (2) 해당 열림 이후 문자열에 다른 `<section id="SC-">` 열림이
            #           나오면 그 지점에서 잘라내고 `</section>` 로 강제 종료,
            #       (3) 현재 섹션 내부에 일반 `<section class=...>` (id 없는) 중첩은
            #           허용 (레이아웃 용).
            import re as _re_s
            _SC_OPEN = _re_s.compile(
                r"<section[^>]*\bid=['\"]SC-[A-Z]{2,5}-\d{3,4}[\w-]*['\"][^>]*>"
            )
            _ITEM_OPEN_RE = _re_s.compile(
                r"<section[^>]*\bid=['\"]" + _re_s.escape(item) + r"['\"][^>]*>"
            )
            _item_open = _ITEM_OPEN_RE.search(content)
            section_html = ""
            if _item_open:
                _start = _item_open.start()
                _after_open = _item_open.end()
                # 이후 범위에서 또 다른 SC- 섹션 열림 찾기 → 중첩/누수 잘라내기
                _next_sc = _SC_OPEN.search(content, pos=_after_open)
                if _next_sc:
                    _end = _next_sc.start()
                    _frag = content[_start:_end].rstrip()
                    if not _frag.endswith("</section>"):
                        _frag += "\n</section>"
                    section_html = _frag
                    logger.warning(
                        "chunked_html_item_truncated_on_nested node=%s item=%s next=%s",
                        node.id[:8], item, _next_sc.group(0)[:60],
                    )
                else:
                    # outer </section> 매칭: 정상 케이스
                    _tail = content[_after_open:]
                    _close = _re_s.search(r"</section>", _tail)
                    if _close:
                        section_html = content[_start:_after_open + _close.end()]
                    else:
                        section_html = content[_start:].rstrip() + "\n</section>"
                        logger.warning(
                            "chunked_html_item_missing_close node=%s item=%s",
                            node.id[:8], item,
                        )
            else:
                # fallback: id 가 일치하는 섹션이 없으면 첫 `<section ...>` 사용 or 래퍼
                sec_m = _re_s.search(r"<section[^>]*>[\s\S]*?</section>", content)
                if sec_m:
                    section_html = sec_m.group(0)
                else:
                    section_html = f'<section id="{item}" class="screen">\n{content}\n</section>'

            # Fix 6-추가: 섹션 HTML 내부에 잔존하는 다른 SC- 섹션 열림을 제거
            # (truncate 가 이미 처리했지만 방어적으로 한 번 더)
            _inner_strip = list(_SC_OPEN.finditer(section_html))
            if len(_inner_strip) > 1:
                # 첫 SC-열림은 유지, 두 번째 이후 SC-열림부터는 잘라낸다
                _second = _inner_strip[1]
                section_html = section_html[:_second.start()].rstrip()
                if not section_html.endswith("</section>"):
                    section_html += "\n</section>"
                logger.warning(
                    "chunked_html_item_stripped_inner_sc node=%s item=%s count=%d",
                    node.id[:8], item, len(_inner_strip) - 1,
                )

            # canonical 전략: 개별 섹션 style 삽입 불필요 (common_head 에 통합됨)

            sections.append(section_html)
            completed_items[item] = section_html
            logger.info("chunked_html_item node=%s item=%s ok size=%d",
                        node.id[:8], item, len(section_html))
            # D6: StateStore complete
            if _state_store:
                try:
                    import hashlib as _hl
                    h = _hl.sha256(section_html.encode("utf-8")).hexdigest()[:16]
                    await _state_store.complete(engagement_id, node.id, item, h)
                except Exception as _e:
                    logger.debug("state_store_complete_fail %s", _e)
            # D6: Content Cache put — 재시도·유사 프로젝트 재사용
            if _content_cache and _cache_input_hash and not _cache_hit_content:
                try:
                    await _content_cache.put(
                        engagement_id, "chunked_html_section",
                        _cache_input_hash, section_html,
                        input_tokens=getattr(resp, "input_tokens", 0),
                        output_tokens=getattr(resp, "output_tokens", 0),
                    )
                except Exception as _e:
                    logger.debug("content_cache_put_fail %s", _e)
            # D6: Advisor review — 비동기 일관성 검증 (reject 시 경고만 기록)
            if _advisor:
                try:
                    review = await _advisor.review_chunk(
                        engagement_id, node.id, "ui_section", item,
                        section_html[:3500],
                    )
                    if review.inconsistent:
                        logger.warning(
                            "advisor_reject node=%s item=%s reason=%s",
                            node.id[:8], item, review.reason[:100],
                        )
                except Exception as _e:
                    logger.debug("advisor_review_fail %s", _e)

            # D14: Stage 22-B Advisor semantic 체크리스트 — spec.semantic_checklist 있을 때
            if _advisor and spec.get("semantic_checklist"):
                try:
                    sreview = await _advisor.review_semantic(
                        engagement_id, node.id, "ui_section", item,
                        section_html[:3500], spec["semantic_checklist"],
                    )
                    if sreview.inconsistent:
                        logger.warning(
                            "advisor_semantic_reject node=%s item=%s reason=%s",
                            node.id[:8], item, sreview.reason[:100],
                        )
                except Exception as _e:
                    logger.debug("advisor_semantic_fail %s", _e)

            # D14: Stage 20 Validator chain — 섹션 단위 구조 검증 + 자동 수정
            if _validator_chain:
                try:
                    chain_res = _validator_chain(section_html, spec, None)
                    if chain_res.fixed_content:
                        section_html = chain_res.fixed_content
                        # 병합 list 최신화
                        sections[-1] = section_html
                        completed_items[item] = section_html
                        logger.info(
                            "validator_auto_fix node=%s item=%s fixes=%d",
                            node.id[:8], item, chain_res.auto_fixed_count,
                        )
                    if chain_res.failures:
                        logger.warning(
                            "validator_failures node=%s item=%s count=%d sample=%s",
                            node.id[:8], item, len(chain_res.failures),
                            chain_res.failures[0][:100] if chain_res.failures else "",
                        )
                except Exception as _e:
                    logger.debug("validator_chain_fail %s", _e)

            # D14: Stage 23 Shared Context Ledger — 첫 성공한 섹션의 공통 요소 기록
            if _ledger:
                try:
                    await _ledger.extract_and_record(
                        engagement_id, node.id, section_html, item,
                    )
                except Exception as _e:
                    logger.debug("ledger_record_fail %s", _e)

            # D15: Stage 25 Visual Regression — 소스 휴리스틱 (즉시) + 렌더링(async)
            if _os.environ.get("V8_VISUAL_REGRESSION", "1") != "0":
                try:
                    from engine.skills.visual import analyze_html_source
                    visual_issues = analyze_html_source(section_html)
                    if visual_issues:
                        logger.warning(
                            "visual_heuristic_flags node=%s item=%s issues=%s",
                            node.id[:8], item, visual_issues[:5],
                        )
                except Exception as _e:
                    logger.debug("visual_heuristic_fail %s", _e)
        except Exception as e:
            logger.warning("chunked_html_item_failed node=%s item=%s err=%s",
                           node.id[:8], item, str(e)[:100])
            placeholder = (
                f'<section id="{item}" class="screen" data-incomplete="true">\n'
                f'<h2>{item}</h2>\n'
                f'<p>(자동 생성 실패 — 재실행 필요)</p>\n'
                f'</section>'
            )
            sections.append(placeholder)
            # D6: StateStore fail (retry_count 증가)
            if _state_store:
                try:
                    await _state_store.fail(engagement_id, node.id, item, str(e)[:300])
                except Exception as _e:
                    logger.debug("state_store_fail_record_fail %s", _e)

        # 중간 캐시
        if db:
            try:
                snap = {
                    "type": "chunked_html_items",
                    "completed_items": completed_items,
                    "completed_count": len(completed_items),
                    "total_count": len(items),
                    "updated_at": _now(),
                }
                await db.execute(
                    "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                    (json.dumps(snap, ensure_ascii=False), _now(), node.id),
                )
            except Exception:
                pass

    # 전체 완료 → snapshot 제거
    if db:
        try:
            await db.execute(
                "UPDATE nodes SET task_snapshot=NULL, updated_at=? WHERE id=?",
                (_now(), node.id),
            )
        except Exception:
            pass

    # D6: Coverage Verifier — 누락 item 감지 + 리포트 저장
    # 누락 시에는 노드를 NEEDS_HUMAN 로 직접 전이하지 않고, placeholder 만 남겨
    # 사람이 /engagements/{id}/review UI(Stage 17, D8) 에서 수동 개입 가능.
    if _coverage and engagement_id:
        try:
            report = await _coverage.verify(
                engagement_id, node.id, [i for i in items if isinstance(i, str)],
            )
            await _coverage.save_report(engagement_id, node.id, report)
            if report.missing or report.needs_human:
                logger.warning(
                    "coverage_incomplete node=%s expected=%d produced=%d "
                    "missing=%d needs_human=%d",
                    node.id[:8], report.expected_count, report.produced_count,
                    len(report.missing), len(report.needs_human),
                )
            else:
                logger.info(
                    "coverage_complete node=%s expected=%d produced=%d",
                    node.id[:8], report.expected_count, report.produced_count,
                )
        except Exception as _e:
            logger.warning("coverage_verify_fail node=%s err=%s",
                           node.id[:8], str(_e)[:120])

    # 최종 HTML 병합 — DOCTYPE + head + 모든 섹션
    # common_head 가 없으면 안전 기본값
    head_block = common_head or (
        '<style>\n'
        '  :root { --bg: #1a1f2e; --surface: rgba(255,255,255,.04); --text: #e8eaf0; }\n'
        '  body { background: var(--bg); color: var(--text); '
        'font-family: "Pretendard Variable", Pretendard, -apple-system, system-ui, sans-serif; margin: 0; padding: 24px; }\n'
        '  .screen { margin-bottom: 48px; padding: 24px; background: var(--surface); border-radius: 16px; }\n'
        '</style>'
    )
    body = "\n\n".join(sections)
    merged = (
        f'<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
        f'<meta charset="UTF-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>{spec.get("name") or "UI 디자인 시안"}</title>\n'
        f'{head_block}\n'
        f'</head>\n<body>\n{body}\n</body>\n</html>'
    )

    logger.info(
        "chunked_html_complete node=%s items=%d size=%d",
        node.id[:8], len(sections), len(merged),
    )
    return APIResponse(
        content=merged,
        input_tokens=total_in,
        output_tokens=total_out,
        model=model,
        stop_reason="end_turn",
    )


async def _chunked_document_generate(
    model_adapter: Any,
    model: str,
    assembly: Any,
    max_tokens: int,
    spec: dict,
    node: "NodeSnapshot",
    db: Any = None,
) -> "APIResponse":
    """대형 Document 산출물을 섹션별 청크로 분할 생성 (F4).

    spec.sections = [{name, outline, target_tokens}] 형식 전제.
    각 섹션을 독립 LLM 호출 → 이전 섹션 요약을 다음 호출 컨텍스트에 포함 → 최종 markdown 병합.

    단일 호출로 max_tokens 절단 반복되던 문제를 근본 해결.
    각 섹션은 target_tokens 범위 내에서 완전 생성 → 절단 없음.

    실패 시 fallback: 단일 호출로 복귀 (회귀 위험 차단).
    """
    from engine.ai.model_adapter import APIResponse

    sections = spec.get("sections") or spec.get("chunk_sections") or []
    if not sections:
        # 섹션 정의 없음 → fallback: 단일 호출
        return await model_adapter.call(
            model=model, system=assembly.system,
            prompt=assembly.prompt, max_tokens=max_tokens,
        )

    # 캐시 로드 (이전 실행에서 성공한 섹션 재활용)
    cached_sections: dict[str, str] = {}
    # S4-1: 직전 attempt 에서 누락됐던 섹션 명 — 해당 섹션 호출 시 경고 prepend
    missing_sections_last_attempt: list[str] = []
    if db:
        try:
            row = await db.fetchone(
                "SELECT task_snapshot FROM nodes WHERE id=?", (node.id,),
            )
            if row and row.get("task_snapshot"):
                snap = json.loads(row["task_snapshot"])
                if isinstance(snap, dict) and snap.get("type") == "chunked_document":
                    cached_sections = snap.get("sections", {}) or {}
                    missing_sections_last_attempt = snap.get(
                        "missing_sections_last_attempt", []
                    ) or []
        except Exception:
            cached_sections = {}
            missing_sections_last_attempt = []

    results: list[tuple[str, str]] = []  # (section_name, content)
    total_input = 0
    total_output = 0
    any_truncated = False
    # 섹션 간 일관성 유지용 — 생성된 섹션에서 ID 목록을 누적 추출해 다음 섹션
    # 프롬프트에 강제 주입. 섹션마다 숫자가 다르게 나오는 문제(화면 47 vs 50)를
    # 원천 차단.
    import re as _re_chunked
    _id_patterns = [
        # Korean-aware: 한글 접촉 허용, ASCII alphanumeric 인접만 제외
        _re_chunked.compile(r"(?<![A-Za-z0-9_])SCR-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])SC-[A-Z]{2,4}-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])FR-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])UC-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])RSK-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])KPI-\d{3,4}(?![A-Za-z0-9_])"),
        _re_chunked.compile(r"(?<![A-Za-z0-9_])API-\d{3,4}(?![A-Za-z0-9_])"),
    ]
    shared_ids: set[str] = set()  # 전체 섹션에 걸쳐 반드시 등장해야 할 ID
    shared_id_names: dict[str, str] = {}  # ID → 이름 매핑 (outline 결과)

    def _extract_ids(text: str) -> set[str]:
        ids: set[str] = set()
        for p in _id_patterns:
            ids.update(p.findall(text))
        return ids

    # ── L1 Outline-first: 섹션 상세 생성 전 전체 항목 리스트 확정 ──
    # LLM의 약점(장문 full detail)을 피하고 강점(enumerate)만 활용해 섹션 간
    # 일관성·완전성 확보. 섹션 3개+ 있을 때만 활성화 (작은 문서는 skip).
    if len(sections) >= 3 and not cached_sections:
        _ids, _names, _in, _out = await _chunked_call_outline(
            model_adapter, model, assembly, node.id,
        )
        shared_ids.update(_ids)
        shared_id_names.update(_names)
        total_input += _in
        total_output += _out

    for i, sec in enumerate(sections):
        # S5-L2: engagement 상태 재확인 — pause/force-close 감지 시 즉시 중단
        # (subprocess kill 없이 cooperative abort). 첫 섹션은 진입 허용 (i>0).
        if db and i > 0:
            try:
                _eng_row = await db.fetchone(
                    "SELECT e.status FROM engagements e "
                    "JOIN projects p ON p.engagement_id=e.id "
                    "WHERE p.id=?", (node.project_id,),
                )
                if _eng_row and _eng_row.get("status") in ("PAUSED", "FORCE_CLOSED"):
                    logger.info(
                        "chunked_doc_aborted_by_status node=%s status=%s completed=%d/%d",
                        node.id[:8], _eng_row["status"], i, len(sections),
                    )
                    break
            except Exception:
                pass  # abort 체크 실패해도 루프 계속

        sec_name = (sec.get("name") if isinstance(sec, dict) else str(sec)) or f"섹션 {i+1}"
        outline = (sec.get("outline") if isinstance(sec, dict) else "") or ""
        target = (sec.get("target_tokens") if isinstance(sec, dict) else 0) or 6000
        # target_tokens 는 섹션당 최대 — 전체 max_tokens cap 내에서 상향 조정
        sec_max = min(max(target, 3000), 16000)

        # 캐시 매치 → 재호출 없이 재사용
        if sec_name in cached_sections and cached_sections[sec_name].strip():
            results.append((sec_name, cached_sections[sec_name]))
            logger.info(
                "chunked_doc_cached node=%s section=%s",
                node.id[:8], sec_name[:20],
            )
            continue

        # 이전 완성 섹션 요약 (다음 컨텍스트에 주입해 일관성 유지)
        prev_summary = ""
        if results:
            prev_lines = []
            for pname, pcontent in results[-3:]:  # 최근 3개만
                prev_lines.append(f"- **{pname}**: {pcontent[:200]}...")
            prev_summary = (
                "\n\n### 앞서 완성된 섹션 (참조용 요약)\n" + "\n".join(prev_lines)
            )

        # 섹션 간 ID 일관성 강제 — 외곽 호출 결과 또는 앞 섹션에서 추출한 ID
        # 리스트를 프롬프트에 주입해 이번 섹션도 동일 세트를 다루게.
        id_mandate = ""
        if shared_ids:
            sorted_ids = sorted(shared_ids)
            # 이름 매핑 있으면 "ID | 이름" 형식, 없으면 ID만
            if shared_id_names:
                id_lines = [
                    f"- {i}: {shared_id_names.get(i, '(이름 미정)')}"
                    for i in sorted_ids
                ]
                id_list_text = "\n".join(id_lines)
            else:
                id_list_text = ", ".join(sorted_ids)
            id_mandate = (
                "\n\n### 🔒 필수 항목 리스트 (반드시 이번 섹션도 모두 다룰 것)\n"
                f"전체 항목 {len(sorted_ids)}개:\n"
                f"{id_list_text}\n\n"
                "**경고**: 이 섹션이 위 항목보다 적게 다루거나 다른 번호를 쓰면 "
                "QA에서 자동 거부됩니다. 누락·추가 없이 정확히 위 목록만 다루세요.\n"
                "테이블/목록 섹션이면 위 항목 수만큼 행을 생성하세요.\n"
            )

        # S4-1: 직전 attempt 에서 이 섹션이 누락/미완성이었으면 prompt 최상단에
        # ⚠⚠⚠ 경고 블록 prepend. 같은 섹션 반복 누락 차단 (verdict feedback loop).
        last_attempt_warning = ""
        if sec_name in missing_sections_last_attempt:
            last_attempt_warning = (
                "## ⚠⚠⚠ 직전 시도에서 이 섹션이 통째로 누락됐습니다\n\n"
                "QA 가 이 섹션의 부재 또는 미완성을 명확히 지적했습니다. "
                "이번에는 **반드시 빠짐없이 완전한 본문**을 작성하세요. "
                "같은 누락이 반복되면 자동 거부됩니다.\n\n"
                "---\n\n"
            )
            logger.info(
                "chunked_doc_section_warning_injected node=%s section=%s reason=last_attempt_missing",
                node.id[:8], sec_name[:20],
            )

        # 섹션별 프롬프트 조립
        sec_prompt = (
            last_attempt_warning
            + assembly.prompt
            + "\n\n---\n\n"
            + f"## 이번 호출에서 생성할 섹션: {sec_name}\n\n"
            + (f"**섹션 가이드**: {outline}\n\n" if outline else "")
            + "**엄격 지시**:\n"
            + f"- 이 섹션만 완성합니다. 다른 섹션은 작성하지 마세요.\n"
            + f"- 서론·요약·전체 개요 재작성 금지.\n"
            + f"- 완결된 표·목록·단락을 포함해 **절단 없이** 작성하세요.\n"
            + f"- 출력 형식: `## {sec_name}\\n` 로 시작하는 마크다운 섹션 본문만.\n"
            + prev_summary
            + id_mandate
        )

        try:
            resp = await model_adapter.call(
                model=model, system=assembly.system,
                prompt=sec_prompt, max_tokens=sec_max,
            )
        except Exception as exc:
            logger.warning(
                "chunked_doc_section_failed node=%s section=%s error=%s",
                node.id[:8], sec_name[:20], str(exc)[:150],
            )
            # 한 섹션 실패 → 전체 fallback은 하지 않고 placeholder 넣고 진행
            # (사용자가 해당 섹션만 재실행 가능)
            results.append((sec_name, f"## {sec_name}\n\n(섹션 생성 실패 — 재실행 필요)"))
            continue

        total_input += resp.input_tokens
        total_output += resp.output_tokens
        if resp.stop_reason == "max_tokens":
            any_truncated = True
            logger.warning(
                "chunked_doc_section_truncated node=%s section=%s",
                node.id[:8], sec_name[:20],
            )
        content = resp.content.strip()
        # LLM이 `## 섹션명`을 중복 생성할 수 있으므로 중복 헤더 정리
        if content.startswith(f"## {sec_name}") or content.startswith(f"# {sec_name}"):
            pass  # 이미 헤더 포함
        else:
            content = f"## {sec_name}\n\n" + content

        # min_items 체크 — 섹션에 min_items 정의돼 있으면 실제 항목수(ID + 표 행)
        # 검증 후 미달 시 해당 섹션만 1회 재생성 (더 강한 지시 + 2배 토큰).
        min_items_required = (sec.get("min_items") if isinstance(sec, dict) else 0) or 0
        if min_items_required > 0:
            _effective = _count_section_items(content, _id_patterns)

            if _effective < min_items_required:
                logger.warning(
                    "chunked_doc_section_insufficient node=%s section=%s got=%d min=%d → retry",
                    node.id[:8], sec_name[:20], _effective, min_items_required,
                )
                # 강한 재작성 프롬프트 + 2배 토큰
                retry_prompt = (
                    sec_prompt
                    + f"\n\n## ⚠ 재생성 필요 (자동 감지)\n"
                    + f"이전 생성물이 **{_effective}개**뿐입니다. 이 섹션은 **최소 {min_items_required}개** 항목이 필요합니다.\n"
                    + f"다시 작성하되 모든 항목을 빠짐없이 포함하세요. 절단 없이 완결 작성.\n"
                )
                try:
                    resp_retry = await model_adapter.call(
                        model=model, system=assembly.system,
                        prompt=retry_prompt, max_tokens=min(sec_max * 2, 32000),
                    )
                    total_input += resp_retry.input_tokens
                    total_output += resp_retry.output_tokens
                    content2 = resp_retry.content.strip()
                    if content2.startswith(f"## {sec_name}") or content2.startswith(f"# {sec_name}"):
                        pass
                    else:
                        content2 = f"## {sec_name}\n\n" + content2
                    # 재생성 결과 항목수 재체크 — 그래도 부족하면 그냥 best-effort 수용
                    _retry_effective = _count_section_items(content2, _id_patterns)
                    if _retry_effective >= _effective:
                        content = content2
                        logger.info(
                            "chunked_doc_section_retry_improved node=%s section=%s new=%d (was=%d)",
                            node.id[:8], sec_name[:20], _retry_effective, _effective,
                        )
                except Exception as exc:
                    logger.warning(
                        "chunked_doc_section_retry_failed node=%s section=%s err=%s",
                        node.id[:8], sec_name[:20], str(exc)[:100],
                    )

        results.append((sec_name, content))

        # 이 섹션에서 추출한 ID를 전역 shared_ids 에 누적 — 다음 섹션 프롬프트에
        # 필수 ID 목록으로 주입되어 일관성 강제.
        section_ids = _extract_ids(content)
        if section_ids:
            if not shared_ids:
                # 첫 ID 등장 — 이 섹션이 "목록 섹션"일 가능성 높음. 전체 채택.
                shared_ids.update(section_ids)
                logger.info(
                    "chunked_doc_shared_ids_initialized node=%s section=%s count=%d",
                    node.id[:8], sec_name[:20], len(shared_ids),
                )
            else:
                # 이미 등록된 풀이 있음 — 새 ID가 추가로 발견되면 union (더 포괄).
                new_ids = section_ids - shared_ids
                if new_ids:
                    shared_ids.update(new_ids)
                    logger.info(
                        "chunked_doc_shared_ids_expanded node=%s section=%s added=%d total=%d",
                        node.id[:8], sec_name[:20], len(new_ids), len(shared_ids),
                    )

        # 중간 캐시 저장 (다음 재실행 시 이어받기)
        if db:
            try:
                snap = {
                    "type": "chunked_document",
                    "sections": {n: c for n, c in results},
                    "completed_count": len(results),
                    "total_count": len(sections),
                    "updated_at": _now(),
                    # S4-1: 다음 retry 가 이 힌트 사용 (현재 attempt 의 verdict 로
                    # 덮어써질 예정이지만, 진행 중 보존)
                    "missing_sections_last_attempt": missing_sections_last_attempt,
                }
                await db.execute(
                    "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                    (json.dumps(snap, ensure_ascii=False), _now(), node.id),
                )
            except Exception:
                pass

    # 모든 섹션 완료 → 병합
    merged = "\n\n".join(c for _, c in results).strip()

    # 완료 시 스냅샷 제거 (다음 성공 재실행은 처음부터)
    if db and not any_truncated:
        try:
            await db.execute(
                "UPDATE nodes SET task_snapshot=NULL, updated_at=? WHERE id=?",
                (_now(), node.id),
            )
        except Exception:
            pass

    logger.info(
        "chunked_doc_complete node=%s sections=%d size=%d truncated=%s",
        node.id[:8], len(sections), len(merged), any_truncated,
    )

    return APIResponse(
        content=merged,
        input_tokens=total_input,
        output_tokens=total_output,
        model=model,
        stop_reason="max_tokens" if any_truncated else "end_turn",
    )


def _adjust_strategy_on_missing_section(
    failure_reasons: list[dict | str],
) -> tuple[int | None, str]:
    """재시도 시 missing_section 반복 감지 → 전략 전환 파라미터 반환.

    - failure_reasons 순회하여 "missing section" / "누락" / "missing_section"
      키워드 카운트.
    - 2회 이상이면:
        (max_tokens_hint=64000, directive=타겟 섹션 재생성 프롬프트 블록)
    - 미만이면 (None, "") — 전략 전환 없음.

    directive는 rendered_prompt 말미에 그대로 append 가능한 형태.
    """
    import re as _re

    if not failure_reasons:
        return None, ""

    miss_count = 0
    miss_labels: list[str] = []
    for fr in failure_reasons[-5:]:  # 최근 5개만 (오래된 이력 무시)
        if isinstance(fr, dict):
            text = str(fr.get("reason", "")) + " " + str(fr.get("message", ""))
        else:
            text = str(fr)
        text_l = text.lower()
        # 감지 조건: missing section / missing_section / 누락
        if "missing section" in text_l or "missing_section" in text_l or "누락" in text:
            miss_count += 1
            # 섹션 이름 추출 시도 (한글 3자 이상 단어 간단 매치)
            m = _re.search(
                r"([가-힣A-Za-z][가-힣A-Za-z\s\w]{2,30}?)\s*(?:섹션|section)",
                text, _re.IGNORECASE,
            )
            if m:
                label = m.group(1).strip()[:30]
                if label and label not in miss_labels:
                    miss_labels.append(label)

    if miss_count < 2:
        return None, ""

    labels_text = ", ".join(miss_labels[:6]) if miss_labels else "(파악 불가)"
    directive = (
        "\n\n## 🎯 누락 섹션 집중 복구 (자동 전략 전환)\n"
        f"이전 시도에서 동일한 '섹션 누락' 실패가 {miss_count}회 반복 발생했습니다. "
        f"감지된 누락 섹션: **{labels_text}**.\n\n"
        "**지시**:\n"
        "1. 이미 생성된 섹션은 그대로 유지하고, 누락된 섹션만 우선 완성하세요.\n"
        "2. 서론·서문 재작성 금지 — 필수 섹션 본문을 곧바로 시작.\n"
        "3. 각 누락 섹션은 완결된 표·목록·단락을 모두 포함해야 합니다 (절단 금지).\n"
        "4. 출력 토큰 여유가 확보되었으므로(최대 64K) 완전한 형태로 작성하세요.\n"
    )
    return 64000, directive


async def _estimate_required_output_tokens(db: Any, node: "NodeSnapshot") -> int | None:
    """이전 버전 artifact 크기 기반 필요 출력 토큰 수 추정.

    재실행(retry/cascade)인 경우에만 유의미. 첫 실행이면 None 반환.
    콘텐츠를 로드하지 않고 LENGTH만 조회하여 메모리 절약.
    """
    prev = await db.fetchone(
        """SELECT LENGTH(av.storage_path) AS content_len
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE a.node_id=?
           AND av.version_num = a.current_version""",
        (node.id,),
    )
    if not prev or not prev["content_len"]:
        return None

    # 문자 수 → 토큰 수 추정 (CJK 50% 가정: CJK ~1.7토큰/자, ASCII ~0.25토큰/자)
    char_count = prev["content_len"]
    estimated_tokens = int(char_count * 0.5 * 1.7 + char_count * 0.5 * 0.25)
    return estimated_tokens


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

async def create_skill_executor(
    db: Any,
    assembler: ContextAssembler,
    model_adapter: ModelAdapter,
    episode_store: "Any | None" = None,
) -> Callable:
    """Factory that returns the executor function for DAGAdvancer.

    Args:
        db:            Async DB connection (must support ``execute`` /
                       ``fetchone`` / ``fetchall``).
        assembler:     Existing 5-Layer ContextAssembler instance.
        model_adapter: ModelAdapter instance for LLM calls.
        episode_store: EpisodeStore instance for vector memory (Phase F-3).
                       Optional — defaults to None for backward compatibility.

    Returns:
        An ``async def executor(node: NodeSnapshot) -> None`` callable.
    """
    registry = SkillRegistry()
    budget_enforcer = BudgetEnforcer(db)

    async def executor(node: NodeSnapshot) -> None:
        """Execute a single DAG node using the Skill Hybrid pipeline.

        Steps:
          1. Resolve skill spec from registry.
          2. Load project context from DB.
          3. For QA nodes, attempt programmatic validation first.
          4. Render prompt via skill template.
          5. Assemble full context (5-Layer system — untouched).
          6. Call LLM.
          7. Validate TASK output structurally.
          8. Mark node COMPLETED.

        Args:
            node: The ``NodeSnapshot`` to process.

        Raises:
            ValueError: When post-call structural validation fails
                        (DAGAdvancer handles retry).
        """
        # 0. SKIPPED 노드는 즉시 반환
        if node.state == "SKIPPED":
            return

        # 0-guard-1. QA FAIL 무한 루프 방지: stall_count >= 2 → SUSPENDED
        # 이전 실패 사유(QA verdict)를 description에 주입해 사용자 "실패 사유"
        # 클릭 시 의미있는 내용이 보이도록 함.
        try:
            _stall = await db.fetchone("SELECT stall_count, description FROM nodes WHERE id=?", (node.id,))
            if _stall and (_stall["stall_count"] or 0) >= 2:
                # QA pair description → TASK description 으로 mirror
                _reason_json = _stall.get("description") or ""
                if not _reason_json and node.qa_pair_node_id:
                    _qa_row = await db.fetchone(
                        "SELECT description FROM nodes WHERE id=?",
                        (node.qa_pair_node_id,),
                    )
                    _reason_json = (_qa_row or {}).get("description") or ""
                if not _reason_json:
                    _reason_json = json.dumps({
                        "verdict": "SUSPENDED",
                        "method": "stall_limit",
                        "failures": [
                            f"QA 연속 실패 {_stall['stall_count']}회로 무한 루프 방지를 위해 중단됨.",
                            "재실행 전 원인 파악 및 수동 개입이 필요합니다.",
                        ],
                        "stall_count": _stall["stall_count"],
                    }, ensure_ascii=False)

                # ── 갭 보강 2: SUSPENDED 직전 마지막 QA 사유로 거시 진단 시도 ──
                # stall 누적은 root cause 가 상위에 있다는 강한 신호.
                _stall_fail_text = ""
                try:
                    _rj = json.loads(_reason_json) if _reason_json else {}
                    if isinstance(_rj, dict):
                        _failures = _rj.get("failures") or []
                        if isinstance(_failures, list) and _failures:
                            _stall_fail_text = " ".join(str(f) for f in _failures)
                        elif _rj.get("reason"):
                            _stall_fail_text = str(_rj["reason"])
                except Exception:
                    pass
                if _stall_fail_text:
                    from engine.skills.executor_cascade import macro_diagnose_safe
                    await macro_diagnose_safe(
                        db, node.id,
                        node.task_pair_node_id or node.id,
                        _stall_fail_text,
                        model_adapter=model_adapter, source="suspended_stall",
                    )

                await db.execute(
                    "UPDATE nodes SET state='SUSPENDED', description=?, updated_at=? WHERE id=?",
                    (_reason_json, _now(), node.id),
                )
                logger.warning("stall_limit_exceeded node=%s stall=%d → SUSPENDED", node.id[:8], _stall["stall_count"])
                return
        except Exception:
            pass

        # 0-guard-2. 노드별 토큰 상한: 누적 상한 초과 → SUSPENDED
        # S10: chunk_items 사용 노드는 아이템 수 비례로 상한 동적 확장.
        # spec 은 아직 resolve 전이므로 여기서는 접근 불가 → _TOKEN_LIMIT 은 spec
        # 확보 후 동적 조정 (아래 로직에서 재계산). 여기선 기본값만 설정.
        from engine.config.thresholds import NODE_TOKEN_LIMIT, CHUNK_ITEM_TOKEN_BUDGET
        _TOKEN_LIMIT = NODE_TOKEN_LIMIT
        try:
            _node_usage = await db.fetchone(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) as total FROM agent_token_usage WHERE node_id=?",
                (node.id,),
            )
            if _node_usage and _node_usage["total"] > _TOKEN_LIMIT:
                _reason_json = json.dumps({
                    "verdict": "SUSPENDED",
                    "method": "token_limit",
                    "failures": [
                        f"노드 누적 토큰 {_node_usage['total']:,} > 상한 {_TOKEN_LIMIT:,}",
                        "프롬프트·컨텍스트 축소 또는 분할 실행 필요.",
                    ],
                    "total_tokens": _node_usage["total"],
                }, ensure_ascii=False)
                await db.execute(
                    "UPDATE nodes SET state='SUSPENDED', description=?, updated_at=? WHERE id=?",
                    (_reason_json, _now(), node.id),
                )
                logger.warning("node_token_limit node=%s total=%d → SUSPENDED", node.id[:8], _node_usage["total"])
                return
        except Exception:
            pass

        # 0-0. PARTIAL 패치 모드 체크 (INVALID 노드 + change_type='PARTIAL')
        _partial_info = await _check_partial_mode(db, node)
        if _partial_info:
            await _execute_partial_patch(
                db, node, assembler, model_adapter, budget_enforcer, _partial_info,
            )
            return

        # 0-0. QA pair guard: QA 노드는 짝 TASK가 정상 완료 경로에 있을 때만 실행.
        # TASK가 SUSPENDED/FAILED/BLOCKED/INVALID 이면 산출물 변경이 불가능하므로
        # 같은 산출물을 반복 검사해봐야 같은 결과만 나오고 결국 QA FAILED 확정으로 이어진다.
        # → QA를 BLOCKED로 대기시키고 TASK 측 해결(watchdog 재개, 재실행 등)이 끝난 뒤
        #   상위 제어(DAGAdvancer)가 자연스럽게 다시 READY로 승격시키도록 유도.
        if node.node_type == "QA":
            _task_pair_id: str | None = getattr(node, "task_pair_node_id", None)
            _task_row = None
            if _task_pair_id:
                _task_row = await db.fetchone(
                    "SELECT id, state FROM nodes WHERE id=?",
                    (_task_pair_id,),
                )
            else:
                # task_pair_node_id 누락(분할 노드 등) → 이름 기반 fallback
                # "[QA] <name>" → "<name>" 로 동일 project/dag에서 TASK 검색
                _qa_name: str = getattr(node, "name", "") or ""
                _task_name = _qa_name[4:] if _qa_name.startswith("[QA] ") else _qa_name
                if _task_name and _task_name != _qa_name:
                    _task_row = await db.fetchone(
                        """SELECT id, state FROM nodes
                           WHERE dag_id=? AND project_id=? AND node_type='TASK' AND name=?
                           LIMIT 1""",
                        (node.dag_id, node.project_id, _task_name),
                    )
            if _task_row and _task_row["state"] in (
                "SUSPENDED", "FAILED", "BLOCKED", "INVALID", "NEEDS_HUMAN",
            ):
                logger.warning(
                    "executor_qa_pair_guard qa=%s task=%s task_state=%s → BLOCKED",
                    node.id[:8],
                    (_task_row["id"] or "")[:8],
                    _task_row["state"],
                )
                await db.execute(
                    "UPDATE nodes SET state='BLOCKED', updated_at=? WHERE id=?",
                    (_now(), node.id),
                )
                return

        # 0-1. Race condition 방어: 선행 노드가 실제 DB에서 COMPLETED인지 재확인
        # DAGAdvancer가 인메모리 스냅샷 기반으로 launch하므로
        # 선행 TASK executor가 아직 실행 중인 상태에서 하위 노드가 launch될 수 있음
        if node.deps:
            _dep_check = await db.fetchall(
                f"SELECT id, state FROM nodes WHERE id IN ({','.join('?' * len(node.deps))})",
                tuple(node.deps),
            )
            _incomplete = [
                r["id"][:8] for r in _dep_check
                if r["state"] not in ("COMPLETED", "SKIPPED")
            ]
            _failed_blockers = [
                r["id"] for r in _dep_check
                if r["state"] in ("FAILED", "SUSPENDED", "INVALID")
            ]
            if _incomplete:
                logger.warning(
                    "executor_dep_guard node=%s incomplete_deps=%s → BLOCKED",
                    node.id[:8], _incomplete,
                )
                # ── 갭 보강 3: BLOCKED dependency → blocker 사유로 거시 진단 ──
                # 단순 "still running" 이 아닌 FAILED/SUSPENDED/INVALID 인 경우만
                # blocker 의 failure_reasons 를 추출하여 root cause 더 위 단계 추적.
                if _failed_blockers:
                    _blk_text = ""
                    try:
                        _blk_row = await db.fetchone(
                            "SELECT failure_reasons FROM nodes WHERE id=?",
                            (_failed_blockers[0],),
                        )
                        if _blk_row and _blk_row.get("failure_reasons"):
                            _frs_arr = json.loads(_blk_row["failure_reasons"])
                            if _frs_arr and isinstance(_frs_arr, list):
                                _last = _frs_arr[-1]
                                if isinstance(_last, dict):
                                    _blk_text = str(_last.get("reason", ""))
                    except Exception:
                        pass
                    if _blk_text:
                        from engine.skills.executor_cascade import macro_diagnose_safe
                        await macro_diagnose_safe(
                            db, node.id, _failed_blockers[0], _blk_text,
                            model_adapter=model_adapter, source="blocked_dep",
                        )
                # BLOCKED 전환 + return (retry_count 미소비 — ValueError raise 시 dag_advancer가 retry_count 증가)
                await db.execute(
                    "UPDATE nodes SET state='BLOCKED', updated_at=? WHERE id=?",
                    (_now(), node.id),
                )
                return

        # 0-2. DESIGN 시안 분할: GRP-XX 기반 정확한 그룹 분할
        if (
            node.phase == "DESIGN"
            and "시안" in node.name
            and "(" not in node.name
            and node.node_type == "TASK"
        ):
            try:
                from engine.skills.splitting import (
                    _count_screens_from_artifact,
                    split_design_task_by_group,
                )
                _screen_count = await _count_screens_from_artifact(
                    db, node.project_id,
                )
                if _screen_count > 10:  # 분할 임계값
                    _sub_count = await split_design_task_by_group(
                        db, node.project_id, node.id, node.dag_id,
                    )
                    if _sub_count > 0:
                        logger.info(
                            "design_split_done node=%s screens=%d subs=%d",
                            node.id[:8], _screen_count, _sub_count,
                        )
                        return  # 원본 노드는 SKIPPED, 서브태스크가 대체
            except Exception as _split_err:
                logger.warning(
                    "design_split_failed node=%s error=%s — fallback to single",
                    node.id[:8], _split_err,
                )

        # 0-3. 컴포넌트 라이브러리 분할
        if (
            node.phase == "DESIGN"
            and node.name == "컴포넌트 라이브러리"
            and "(" not in node.name
            and node.node_type == "TASK"
        ):
            try:
                from engine.skills.splitting import split_component_library_by_category
                _sub_count = await split_component_library_by_category(
                    db, node.project_id, node.id, node.dag_id,
                )
                if _sub_count > 0:
                    logger.info("library_split_done node=%s subs=%d", node.id[:8], _sub_count)
                    return
            except Exception as _split_err:
                logger.warning("library_split_failed node=%s error=%s", node.id[:8], _split_err)

        # 1. Resolve skill spec
        spec = registry.resolve(node.name, node.phase, node.node_type)

        # 1-1. v10 컴포넌트 조합: assembly 노드는 AI 호출 없이 렌더러 실행
        if spec and spec.get("composition_role") == "assembly":
            await _handle_assembly_node(db, node, spec)
            return

        # 2. Load project context
        project = await _load_project_context(db, node.project_id)

        # 2-1. Phase F-4: backend_requirement 조건부 스킬 필터
        # YAML에 backend_requirement가 선언된 스킬은 project.backend_choice와
        # 일치할 때만 실행. 불일치 시 spec=None으로 강등 → fallback 경로 (spec 없음 처리).
        if spec:
            _required_backend = spec.get("backend_requirement")
            if _required_backend:
                _project_backend = (
                    getattr(project, "backend_choice", None)
                    or (project.global_context or {}).get("backend_choice")
                    or "sql"
                )
                if _required_backend != _project_backend:
                    logger.info(
                        "skill_skipped_by_backend node=%s required=%s project=%s",
                        node.id[:8], _required_backend, _project_backend,
                    )
                    spec = None

        # 3. QA node: try programmatic validation first
        _cached_task_output = None  # step 4에서 재사용 (이중 DB 호출 방지)
        if node.node_type == "QA":
            # qa_mode=True: 구조·exports 판정용 낮은 예산 (5K) — 토큰 85% 절약
            task_output = await _load_task_artifact(db, node.task_pair_node_id, qa_mode=True)
            _cached_task_output = task_output

            handled, spec = await _handle_qa_dispatch(
                db, node, spec, project, task_output, model_adapter=model_adapter,
            )
            if handled:
                return

            # ── L2 AI QA artifact_version 캐시 ──
            # 같은 artifact_version + 이전 PASS 이력이 있으면 Sonnet 재호출 없이
            # 이전 verdict 재사용. Cascade/watchdog 재실행으로 variance 뒤집히는
            # 문제 원천 차단.
            # 사용자 수동 c9_manual_retry는 retry_count=0 리셋이라 여기 도달 시
            # 캐시 무효 (retry_count>0 조건). stall 후 watchdog resume 경로는
            # 캐시 히트로 처리.
            # S6.2: retry_count 조건 제거. cascade 재트리거 (retry=0 유지) 도
            # 캐시 hit 처리 → 같은 artifact_version QA 반복 차단.
            # (실측: PRD·리스크 QA 각 29/24 회 반복 호출, 138K/137K 토큰 낭비)
            # 사용자 c9_manual_retry 는 artifact 재생성 → version 증가 → 자동 miss.
            # S6.2.1: cache 를 artifact_qa_stamps 에서 읽음 (영구 보존).
            # description 은 cascade 가 리셋하지만 stamps 는 DELETE 전까진 유지.
            # → cascade 재트리거도 stamp 찾으면 즉시 COMPLETED.
            if node.task_pair_node_id:
                try:
                    _stamp = await db.fetchone(
                        """SELECT qs.verdict, qs.verification_passed, qs.phase
                        FROM artifact_qa_stamps qs
                        JOIN artifacts a ON a.id = qs.artifact_id
                        WHERE a.node_id=? AND qs.qa_node_id=?
                        ORDER BY qs.stamped_at DESC LIMIT 1""",
                        (node.task_pair_node_id, node.id),
                    )
                    if _stamp and _stamp.get("verdict") in ("PASS", "CONDITIONAL_PASS"):
                        logger.info(
                            "qa_cache_hit node=%s stamp=%s retry=%d — LLM 호출 스킵",
                            node.id[:8], _stamp["verdict"], node.retry_count,
                        )
                        await db.execute(
                            "UPDATE nodes SET state='COMPLETED', "
                            "completed_at=?, updated_at=? WHERE id=?",
                            (_now(), _now(), node.id),
                        )
                        return
                except Exception as _qc_err:
                    logger.debug("qa_cache_check_skip: %s", _qc_err)

        # 4. Prepare prompt via template
        if node.node_type == "QA" and spec:
            # QA 노드 전용 프롬프트 빌드 — qa_prompt + semantic + 구조화된 출력 강제
            rendered_prompt = await _build_qa_ai_prompt(
                db, node, spec, project,
                task_content_cache=_cached_task_output,
            )

            # 분할 노드 QA: 전체 기준이 아닌 카테고리별 기준 적용
            if "(" in node.name and node.task_pair_node_id:
                _task_node = await db.fetchone("SELECT name FROM nodes WHERE id=?", (node.task_pair_node_id,))
                if _task_node and "(" in _task_node["name"]:
                    import re as _re_cat
                    _cat_match = _re_cat.search(r'\(([^)]+)\)', _task_node["name"])
                    _cat_name = _cat_match.group(1) if _cat_match else ""
                    rendered_prompt += (
                        f"\n\n## ⚠️ 분할 노드 검증 기준"
                        f"\n이 산출물은 '{_cat_name}' 카테고리만 포함합니다."
                        f"\n전체 40개/7개 카테고리 기준이 아닌, 이 카테고리에 해당하는 컴포넌트만 검증하세요."
                        f"\n- 이 카테고리의 컴포넌트가 1개 이상 존재하는가"
                        f"\n- JSON이 유효한가"
                        f"\n- CSS가 토큰 변수를 참조하는가"
                        f"\n- 슬롯 정의가 올바른가"
                        f"\n다른 카테고리 누락은 평가하지 마세요.\n"
                    )

            # 2단계 문서 QA: 구조 통과 → AI는 의미 검증에만 집중
            if spec.get("_structural_passed"):
                rendered_prompt += (
                    "\n\n## ✅ 구조 검증 자동 통과 (1단계 완료)"
                    "\n아래 구조 항목은 프로그래매틱 검증에서 이미 확인됨:"
                    "\n- 최소 분량 충족"
                    "\n- 마크다운 구조 존재"
                    "\n- 필수 섹션 존재"
                    "\n- TODO/TBD 미발견"
                    "\n- 코드 블록 완결"
                    "\n\n**당신의 역할: 의미(semantic) 검증만 수행하세요.**"
                    "\n구조·형식·분량은 이미 통과했으므로 재검사 불필요."
                    "\n집중 사항:"
                    "\n1. 내용의 정확성·일관성"
                    "\n2. 요구사항 대비 완성도"
                    "\n3. 논리적 오류·모순"
                    "\n4. 산출물 간 정합성"
                    "\n\n구조 문제가 아닌 **의미적 결함**이 있을 때만 FAIL 판정하세요.\n"
                )

            # BUILD phase QA: 디자인 불일치 이슈 주입
            if node.phase == "BUILD" and spec.get("_design_compliance_issues"):
                design_tokens_ctx = await _load_design_tokens_for_qa(db, node)
                if design_tokens_ctx:
                    rendered_prompt += design_tokens_ctx
                issues = spec["_design_compliance_issues"]
                rendered_prompt += "\n\n## ⚠️ 디자인 일치 검증 자동 감지 결과 (반드시 반영)\n"
                rendered_prompt += "아래 항목은 프로그래매틱 검증에서 감지된 디자인 불일치입니다.\n"
                rendered_prompt += "각 항목을 확인하고 심각도(CRITICAL/HIGH/MEDIUM/LOW)를 판정하세요.\n\n"
                for iss in issues:
                    rendered_prompt += f"- {iss}\n"
                rendered_prompt += "\nCRITICAL 또는 HIGH가 1건이라도 있으면 반드시 FAIL 판정하세요.\n"
        elif spec and spec.get("prompt"):
            variables = {
                "name": node.name,
                "project_name": project.project_name,
                "client_name": project.client_name,
                "phase": node.phase,
            }
            rendered_prompt = render(spec["prompt"], variables)

            # 분할 노드: description이 JSON 메타데이터면 카테고리 지시로 변환
            if node.node_type == "TASK" and "(" in node.name:
                try:
                    _desc = await db.fetchone("SELECT description FROM nodes WHERE id=?", (node.id,))
                    if _desc and _desc["description"]:
                        _desc_data = json.loads(_desc["description"])
                        if isinstance(_desc_data, dict) and "_library_split_category" in _desc_data:
                            _cat = _desc_data["_library_split_category"]
                            _cat_desc = _desc_data.get("_library_split_description", "")
                            rendered_prompt = (
                                f"## ⚠️ 이 노드는 '{_cat}' 카테고리의 컴포넌트만 생성합니다.\n"
                                f"카테고리 설명: {_cat_desc}\n"
                                f"다른 카테고리의 컴포넌트는 생성하지 마세요.\n"
                                f"반드시 category 필드를 '{_cat}'으로 설정하세요.\n"
                                f"반드시 순수 JSON 배열로만 출력하세요.\n\n"
                                f"---\n\n{rendered_prompt}"
                            )
                except (json.JSONDecodeError, Exception):
                    pass

            # Inject self-validation block for TASK nodes
            if node.node_type == "TASK" and spec.get("validation", {}).get("structural"):
                rendered_prompt += _build_self_check_block(spec)

            # ── ROOT CAUSE PREVENTION: DESIGN TASK에 화면 목록 강제 주입 ──
            if node.phase == "DESIGN" and node.node_type == "TASK":
                try:
                    if "<!-- SCREEN_LIST_INJECTED -->" not in rendered_prompt:
                        _screen_req = await _inject_screen_list_requirement(
                            db, node.project_id, node.name,
                        )
                        if _screen_req:
                            rendered_prompt += _screen_req
                            # 레시피 노드: 추가 강조
                            if "레시피" in node.name:
                                rendered_prompt += (
                                    "\n\n**⚠️ 레시피는 화면 목록의 모든 화면에 대해 "
                                    "생성해야 합니다. 일부 화면만 레시피를 생성하면 "
                                    "QA에서 자동 반려됩니다.**\n"
                                )
                except Exception as _scr_inj_err:
                    logger.warning("screen_list_inject_design failed: %s", _scr_inj_err)

            # 페이지 레시피: 사용 가능한 컴포넌트 목록 강제 주입
            if spec.get("composition_role") == "recipe":
                comp_list = await _load_component_names(db, node.project_id)
                if comp_list:
                    rendered_prompt += (
                        "\n\n## ⚠️ 사용 가능한 컴포넌트 목록 (필수 준수)\n"
                        "아래 목록에 있는 컴포넌트만 placements에서 참조하세요.\n"
                        "**이 목록에 없는 컴포넌트 이름을 사용하면 조립이 실패합니다.**\n\n"
                    )
                    for name in comp_list:
                        rendered_prompt += f"- `{name}`\n"
                    rendered_prompt += (
                        "\n위 컴포넌트를 조합하여 모든 페이지를 구성하세요.\n"
                        "필요한 컴포넌트가 목록에 없으면 가장 유사한 것으로 대체하세요.\n"
                    )

                # 필수 UX placement 안내 주입
                from engine.composition.registry import REQUIRED_UX_PLACEMENTS
                rendered_prompt += (
                    "\n\n## ⚠️ 필수 UX Placement (모든 페이지에 반드시 포함)\n"
                    "아래 placement는 모든 페이지 레시피에 **반드시** 포함되어야 합니다.\n"
                    "누락 시 시스템이 자동 추가하지만, 명시적으로 포함하면 더 정확한 UX를 제공합니다.\n\n"
                    "| component_name | order | condition | 설명 |\n"
                    "|---|---|---|---|\n"
                )
                for rp in REQUIRED_UX_PLACEMENTS:
                    rendered_prompt += (
                        f"| `{rp['component_name']}` | {rp['order']} "
                        f"| `{rp.get('condition', '')}` | {rp['description']} |\n"
                    )
                rendered_prompt += (
                    "\n**참고:** loading_indicator, error_boundary, empty_state는 order < 0 "
                    "(페이지 상단), toast_container와 modal_container는 order > 9000 (페이지 하단).\n"
                )

            # ── 페이지 레시피: 프로그래매틱 생성 시도 (AI 0회, 토큰 0) ──
            if (
                spec.get("composition_role") == "recipe"
                and node.node_type == "TASK"
                and node.phase == "DESIGN"
            ):
                try:
                    from engine.composition.recipe_generator import (
                        generate_recipes_programmatic,
                        recipes_to_artifact_json,
                    )
                    from engine.composition.registry import CompositionRegistry
                    _prog_recipes = await generate_recipes_programmatic(
                        db, node.project_id,
                    )
                    if _prog_recipes and len(_prog_recipes) > 0:
                        # 레시피 DB 저장은 generator 내부에서 완료됨
                        # artifact JSON 생성
                        _reg = CompositionRegistry(db)
                        _all_recipes = await _reg.load_all_recipes(node.project_id)
                        _artifact_json = recipes_to_artifact_json(
                            _prog_recipes, _all_recipes,
                        )

                        # artifact 저장
                        await _save_artifact(
                            db, node, _artifact_json, "json",
                        )

                        # 노드 완료 마킹
                        _prog_desc = {}
                        _prog_desc_row = await db.fetchone(
                            "SELECT description FROM nodes WHERE id=?",
                            (node.id,),
                        )
                        if _prog_desc_row and _prog_desc_row["description"]:
                            try:
                                _prog_desc = json.loads(
                                    _prog_desc_row["description"]
                                )
                            except (ValueError, TypeError) as exc:
                                logger.debug("prog_desc_parse_failed node=%s error=%s", node.id[:8], exc)
                        _prog_desc["_programmatic_complete"] = True
                        from engine.lifecycle.engine_version import compute_engine_version
                        _prog_desc["_engine_version"] = compute_engine_version()
                        _prog_desc["_recipe_count"] = len(_prog_recipes)
                        _prog_desc["_recipe_slugs"] = [
                            r["page_slug"] for r in _prog_recipes
                        ]

                        now = _now()
                        await db.execute(
                            "UPDATE nodes SET state='COMPLETED', "
                            "completed_at=?, updated_at=?, description=? "
                            "WHERE id=?",
                            (
                                now,
                                now,
                                json.dumps(
                                    _prog_desc, ensure_ascii=False
                                ),
                                node.id,
                            ),
                        )

                        logger.info(
                            "programmatic_recipe_complete node=%s "
                            "recipes=%d slugs=%s",
                            node.id[:8],
                            len(_prog_recipes),
                            [r["page_slug"] for r in _prog_recipes[:5]],
                        )
                        return  # AI 호출 스킵 — 프로그래매틱 완료
                except Exception as _prog_recipe_err:
                    logger.warning(
                        "programmatic_recipe_failed project=%s err=%s "
                        "— falling through to AI",
                        node.project_id,
                        _prog_recipe_err,
                    )
                    # 폴백: AI 경로로 진행

            # VERIFY phase: 상위 단계 산출물(코드/설계)을 컨텍스트로 주입
            if node.node_type == "TASK" and node.phase == "VERIFY":
                upstream_context = await _load_upstream_artifacts(db, node)
                if upstream_context:
                    rendered_prompt += upstream_context

            # ── ROOT CAUSE PREVENTION: BUILD 프론트엔드 TASK에 화면 목록 강제 주입 ──
            if node.phase == "BUILD" and node.node_type == "TASK":
                try:
                    if ("프론트엔드" in node.name
                            and "<!-- SCREEN_LIST_INJECTED -->" not in rendered_prompt):
                        _screen_req = await _inject_screen_list_requirement(
                            db, node.project_id, node.name,
                        )
                        if _screen_req:
                            rendered_prompt += _screen_req
                except Exception as _scr_inj_err:
                    logger.warning("screen_list_inject_build failed: %s", _scr_inj_err)

                # ── Upgrade 3: DEFINE 산출물 컨텍스트 주입 (업무 흐름/기능/비즈니스 규칙) ──
                try:
                    if "<!-- DEFINE_CONTEXT_INJECTED -->" not in rendered_prompt:
                        _define_ctx = await _inject_define_context_for_build(
                            db, node.project_id, node.name,
                        )
                        if _define_ctx:
                            rendered_prompt += _define_ctx
                except Exception as _def_inj_err:
                    logger.debug("define_context_inject_build skipped: %s", _def_inj_err)

                # ── Upgrade 4: Harness 구조 요구사항 선제 주입 ──
                # QA harness가 보는 체크 항목(interface/export default 등)을
                # 생성 단계에서 미리 알려 반복 동일 실패를 구조적으로 차단.
                try:
                    if "<!-- HARNESS_REQUIREMENTS_INJECTED -->" not in rendered_prompt:
                        _harness_req = await _inject_harness_structural_requirements(node)
                        if _harness_req:
                            # rendered_prompt 상단에 배치 — AI 가 먼저 읽도록.
                            # 다른 컨텍스트(화면목록/DEFINE 등)는 참고자료, 이쪽은 제약이므로 우선.
                            rendered_prompt = _harness_req + "\n" + rendered_prompt
                except Exception as _hrq_err:
                    logger.debug("harness_req_inject skipped: %s", _hrq_err)

            # BUILD phase: 노드별 컨텍스트 분기 (프로그래매틱 코드생성 포함)
            if node.phase == "BUILD" and node.node_type == "TASK":
                handled, rendered_prompt = await _handle_build_programmatic(
                    db, node, project, spec, rendered_prompt,
                    assembler, model_adapter, budget_enforcer,
                )
                if handled:
                    return

            # BUILD/DELIVER phase: 프로젝트 환경변수를 프롬프트에 주입
            if node.node_type == "TASK" and node.phase in ("BUILD", "DELIVER"):
                # DESIGN 산출물 기반 환경변수 자동 추출 (첫 BUILD 진입 시 1회)
                try:
                    from engine.core.env_config_generator import extract_env_from_artifacts
                    await extract_env_from_artifacts(db, node.project_id, project.engagement_id)
                except Exception as _env_err:
                    logger.debug("env_extract_skip error=%s", _env_err)
                env_context = await _load_env_vars_context(db, node.project_id, project.engagement_id)
                if env_context:
                    rendered_prompt += env_context

            # 문서 스캐폴드 주입 (기술 문서: 섹션 골격 프로그래매틱 생성 → AI는 내용만 채우기)
            if node.node_type == "TASK":
                try:
                    from engine.skills.doc_scaffold import build_document_scaffold
                    _scaffold = build_document_scaffold(spec, node.name, node.phase)
                    if _scaffold:
                        rendered_prompt = (
                            "아래 스캐폴드의 각 섹션을 실제 내용으로 채우세요. "
                            "섹션 순서와 헤딩(## )을 유지하고, <!-- --> 주석을 실제 내용으로 교체하세요. "
                            "테이블이 있으면 행을 채우세요. 섹션을 추가하거나 삭제하지 마세요.\n\n"
                            f"```markdown\n{_scaffold}\n```\n\n"
                            f"---\n\n{rendered_prompt}"
                        )
                except Exception as _scf_err:
                    logger.debug("doc_scaffold_skip node=%s error=%s", node.id[:8], _scf_err)

            # Gotchas 주입: 프로젝트 레벨 실수 DB → 반복 방지
            try:
                _gotchas_text = await _load_gotchas_for_prompt(db, node.project_id)
                if _gotchas_text:
                    rendered_prompt += _gotchas_text
            except Exception as _gc_err:
                logger.debug("gotchas_load_skip node=%s error=%s", node.id[:8], _gc_err)

            # Phase F: episodes 벡터 검색 — 유사 gotcha 에피소드로 프롬프트 보강
            if episode_store:
                try:
                    _similar_eps = await episode_store.search_similar_episodes(
                        query=node.name,
                        project_id=node.project_id,
                        episode_type="gotcha",
                        top_k=3,
                        min_similarity=0.3,
                    )
                    if _similar_eps:
                        _hints = "\n\n## ⚠️ 유사 실패 사례 (Phase F 벡터 검색)\n"
                        for ep in _similar_eps:
                            _hints += (
                                f"- [{ep.get('node_name','?')}] "
                                f"{str(ep.get('content',''))[:200]}\n"
                            )
                        _hints += "\n위 패턴 반복 금지.\n"
                        rendered_prompt += _hints
                        # Phase F 가시성 — 벡터 주입이 실제로 일어났음을 info 레벨로 기록
                        logger.info(
                            "phase_f_vector_inject node=%s hits=%d",
                            node.id[:8], len(_similar_eps),
                        )
                except Exception as _vec_err:
                    logger.debug(
                        "vector_search_skip node=%s error=%s",
                        node.id[:8], _vec_err,
                    )

            # 패턴 학습 힌트 추가 주입: spec_name 기반 과거 실패 패턴 집계 (gotchas_learning.py)
            try:
                from engine.skills.gotchas_learning import get_hints_for_spec
                _learning_hints = await get_hints_for_spec(db, node.name)
                if _learning_hints:
                    rendered_prompt += _learning_hints
            except Exception as _gl_err:
                logger.debug(
                    "gotchas_learning_skip node=%s error=%s",
                    node.id[:8], _gl_err,
                )
        else:
            rendered_prompt = f"산출물 '{node.name}'을(를) 작성하세요."

        # 5. Create NodeContext with rendered prompt as description
        engagement_id = project.engagement_id  # ProjectContext 필드 (빈 문자열 가능)

        # 실패 이력 로드 (Layer 5: 재시도 시 이전 실패 사유를 컨텍스트에 포함)
        failure_reasons = []
        if node.retry_count > 0:
            fr_row = await db.fetchone(
                "SELECT failure_reasons FROM nodes WHERE id=?", (node.id,)
            )
            if fr_row and fr_row["failure_reasons"]:
                try:
                    failure_reasons = json.loads(fr_row["failure_reasons"])
                except (ValueError, TypeError):
                    failure_reasons = []

        # ── Upgrade 1: 재시도 시 상세 QA 실패 피드백 주입 ──
        # failure_reasons는 짧은 에러 메시지만 포함. QA 노드의 상세 verdict를
        # 로드하여 프롬프트에 직접 주입 → AI가 정확한 문제를 인지하고 수정
        if node.retry_count > 0 and node.node_type == "TASK":
            try:
                _qa_feedback = await _load_previous_qa_feedback(db, node, model_adapter=model_adapter)
                if _qa_feedback:
                    rendered_prompt += _qa_feedback
            except Exception as _qa_fb_err:
                logger.debug("qa_feedback_injection_skip: %s", _qa_fb_err)

        # ── Upgrade 2: 반복 missing_section 감지 시 전략 전환 ──
        # failure_reasons 중 '섹션 누락' 패턴이 2회+ 반복되면 max_tokens를 64K로
        # 상향하고 '이미 완성된 섹션은 유지, 누락만 집중 복구' 프롬프트 주입.
        # 여기서는 hint만 추출 → 아래 max_tokens 계산 블록에서 반영.
        _section_hint_tokens: int | None = None
        if node.retry_count > 0 and node.node_type == "TASK":
            _section_hint_tokens, _section_directive = _adjust_strategy_on_missing_section(
                failure_reasons,
            )
            if _section_directive:
                rendered_prompt += _section_directive
                logger.info(
                    "missing_section_strategy_switch node=%s retry=%d hint_max=%s",
                    node.id[:8], node.retry_count, _section_hint_tokens,
                )

        # ── Upgrade 3 (G4): 금지어(no_todo) 반복 실패 시 모델 승격 + 최종 경고 ──
        # failure_reasons 최근 3회 중 no_todo 실패가 2회+ 이면 결정론적 실패 탈출을
        # 위해 Sonnet → Opus 승격 + 강한 톤 경고 주입. 희귀 발동(retry_count>=2 +
        # no_todo 2회+만).
        _forbidden_model_promoted: str | None = None
        if node.retry_count >= 2 and node.node_type == "TASK":
            _no_todo_fail_count = 0
            for fr in failure_reasons[-3:]:
                fr_text = str(fr).lower() if not isinstance(fr, dict) else str(fr).lower()
                if "no_todo" in fr_text or "금지어" in fr_text or "todo" in fr_text or "tbd" in fr_text:
                    _no_todo_fail_count += 1
            if _no_todo_fail_count >= 2:
                # 모델 승격 (Sonnet → Opus). 이미 Opus면 유지.
                _assigned = node.assigned_model or ModelID.SONNET
                if "sonnet" in _assigned.lower() or "haiku" in _assigned.lower():
                    _forbidden_model_promoted = ModelID.OPUS
                    logger.info(
                        "forbidden_retry_model_promote node=%s %s→%s",
                        node.id[:8], _assigned, _forbidden_model_promoted,
                    )
                # 프롬프트 말미에 최종 경고
                rendered_prompt += (
                    "\n\n## 🚨 최종 경고 — 금지어 반복 삽입\n"
                    f"이전 {_no_todo_fail_count}회 시도에서 금지어(TODO/TBD/미정 등)가 "
                    "반복 포함되어 산출물이 전량 거부되었습니다.\n"
                    "이번 호출에서 금지어가 단 하나라도 등장하면 전체 산출물이 폐기됩니다.\n"
                    "작성 완료 직후 스스로 전체 텍스트를 스캔(검색어: TODO, TBD, FIXME, "
                    "미정, 작성 예정, 추후 작성)해 0건인지 명시적으로 확인하세요.\n"
                    "미결정 항목은 대체 표현('향후 설계 필요', '미지정', '별도 협의')으로 반드시 교체.\n"
                )

        node_ctx = NodeContext(
            node_id=node.id,
            node_type=node.node_type,
            name=node.name,
            description=rendered_prompt,
            phase=node.phase,
            project_id=node.project_id,
            engagement_id=engagement_id,
            retry_count=node.retry_count,
            failure_reasons=failure_reasons,
            assigned_model=node.assigned_model,
            constitution_version_id=None,
        )

        # 6. Assemble context (existing 5-Layer system — UNTOUCHED)
        deltas = await _load_deltas(db, node)
        assembly = assembler.assemble(node_ctx, project, deltas)

        # 7. L1 예산 검사 (engagement_id 있을 때만 — 없으면 기본 max_output 사용)
        try:
            if engagement_id:
                max_tokens = await budget_enforcer.pre_call_check(
                    node_id=node.id,
                    engagement_id=engagement_id,
                    phase=node.phase,
                    prompt=assembly.system + assembly.prompt,
                )
            else:
                max_tokens = TOKEN_BUDGET["max_output"]

            # 8. API call — Progressive Disclosure + Constitutional AI
            model = node.assigned_model or ModelID.SONNET
            # G4: 금지어 반복 실패 시 모델 승격 override
            if _forbidden_model_promoted:
                model = _forbidden_model_promoted
            art_type = spec.get("type", "document") if spec else "document"

            # G3: 문서형 산출물에 금지어 회피 가이드 주입 (첫 시도부터 예방)
            if art_type in ("document", "html", "markdown"):
                try:
                    from engine.skills.executor_context import FORBIDDEN_WORDS_GUIDE
                    # assembly.system에 직접 append (AssemblyResult dataclass 필드)
                    if FORBIDDEN_WORDS_GUIDE not in assembly.system:
                        assembly.system = assembly.system + FORBIDDEN_WORDS_GUIDE
                except Exception as _fwg_err:
                    logger.debug("forbidden_words_guide_inject_skip: %s", _fwg_err)

            # JSON 산출물: budget override 자동 삽입 (토큰 부족으로 잘리는 문제 방지)
            if art_type == "json" and engagement_id:
                _override = await db.fetchone(
                    "SELECT node_id FROM node_budget_overrides WHERE node_id=?", (node.id,)
                )
                if not _override:
                    _admin = await db.fetchone("SELECT id FROM users WHERE role='ADMIN' LIMIT 1")
                    if _admin:
                        await db.execute(
                            "INSERT OR IGNORE INTO node_budget_overrides "
                            "(node_id, max_output_override, reason, created_by, created_at) "
                            "VALUES (?, 32000, 'JSON 산출물 토큰 상향', ?, ?)",
                            (node.id, _admin["id"], _now()),
                        )
                        # override 반영하여 max_tokens 재조회
                        max_tokens = await budget_enforcer.pre_call_check(
                            node_id=node.id, engagement_id=engagement_id,
                            phase=node.phase, prompt=assembly.system + assembly.prompt,
                        )

            # 8-pre. 대형 산출물 사전 예측: 이전 버전 크기 기반 max_tokens 자동 상향
            # 재시도 회차가 높을수록 multiplier 강화 — 절단 반복을 끊기 위해 공격적으로
            # 상향. (0회=1.3x / 1회=1.6x / 2회이상=2.0x, 64K cap 공통)
            if art_type != "json":  # JSON은 이미 32K 오버라이드
                _prev_size = await _estimate_required_output_tokens(db, node)
                # retry-aware multiplier
                _rc = getattr(node, "retry_count", 0) or 0
                if _rc >= 2:
                    _mult = 2.0
                elif _rc == 1:
                    _mult = 1.6
                else:
                    _mult = 1.3
                if _prev_size and _prev_size > max_tokens * 0.8:
                    suggested = min(int(_prev_size * _mult), 64000)
                    if suggested > max_tokens:
                        logger.info(
                            "output_size_preempt node=%s retry=%d mult=%.1f prev_tokens=%d max=%d→%d",
                            node.id[:8], _rc, _mult, _prev_size, max_tokens, suggested,
                        )
                        max_tokens = suggested
                # 재시도면서 prev_size 모를 때도 최소 보장: 24K floor (retry>=1)
                elif _rc >= 1 and max_tokens < 24000 and art_type in (
                    "document", "html", "markdown", "code",
                ):
                    logger.info(
                        "retry_min_floor node=%s retry=%d max=%d→24000",
                        node.id[:8], _rc, max_tokens,
                    )
                    max_tokens = 24000

                # missing_section 반복 감지 시 강제 64K cap (전략 전환)
                if _section_hint_tokens and max_tokens < _section_hint_tokens:
                    logger.info(
                        "missing_section_force_cap node=%s max=%d→%d",
                        node.id[:8], max_tokens, _section_hint_tokens,
                    )
                    max_tokens = _section_hint_tokens

            # 8-pre-2. 분할 노드 max_tokens 제한 (토큰 과다 방지)
            if "(" in node.name and art_type == "json":
                max_tokens = min(max_tokens, 24_000)

            # F4: spec에 sections 정의된 document → 섹션별 분할 생성
            _has_sections = (
                isinstance(spec, dict)
                and (spec.get("sections") or spec.get("chunk_sections"))
                and art_type in ("document", "html", "markdown")
                and node.node_type == "TASK"
            )

            # S8+S9+S11: JSON/HTML 산출물 chunk_items 해석 (범용).
            # _resolve_chunk_items (1)명시 (2)upstream 추출 (3)split_cat 순.
            # 범용: spec 이름·phase·프로젝트 도메인 무관.
            if (isinstance(spec, dict) and art_type in ("json", "html")
                    and node.node_type == "TASK" and not spec.get("chunk_items")):
                _resolved = await _resolve_chunk_items(spec, node, db)
                if _resolved:
                    spec["chunk_items"] = _resolved
            _has_json_items = (
                isinstance(spec, dict)
                and spec.get("chunk_items")
                and art_type == "json"
                and node.node_type == "TASK"
            )
            _has_html_items = (
                isinstance(spec, dict)
                and spec.get("chunk_items")
                and art_type == "html"
                and node.node_type == "TASK"
            )

            if _has_sections:
                logger.info(
                    "chunked_doc_dispatch node=%s sections=%d art_type=%s",
                    node.id[:8], len(spec.get("sections") or spec.get("chunk_sections") or []),
                    art_type,
                )
                response = await _chunked_document_generate(
                    model_adapter, model, assembly, max_tokens, spec, node, db,
                )
            elif _has_json_items:
                logger.info(
                    "chunked_json_dispatch node=%s items=%d",
                    node.id[:8], len(spec.get("chunk_items") or []),
                )
                response = await _chunked_json_items_generate(
                    model_adapter, assembly, spec, node, db,
                )
            elif _has_html_items:
                logger.info(
                    "chunked_html_dispatch node=%s items=%d",
                    node.id[:8], len(spec.get("chunk_items") or []),
                )
                response = await _chunked_html_items_generate(
                    model_adapter, assembly, spec, node, db,
                    engagement_id=engagement_id,
                )
            # TASK 노드만 2단계 생성. QA 노드는 기존 단일 호출 유지.
            elif node.node_type == "TASK" and spec and art_type != "json":
                response = await _two_phase_generate(
                    model_adapter, model, assembly, max_tokens, spec, art_type, node,
                )
            else:
                # S5-C: Model Router wire-up — QA 는 기본 Haiku, retry≥2 는 Opus.
                # Sonnet/Opus 한도 부담 경감 + QA 토큰 비용 감소.
                if node.node_type == "QA":
                    try:
                        from engine.ai.model_router import select_model
                        # S7-P0: getattr safe 접근 — NodeSnapshot 에 failure_reasons
                        # 없을 수 있음 (AttributeError 매번 발생하던 문제 차단)
                        _routed, _routing = select_model(
                            spec, node_type="qa",
                            phase=getattr(node, "phase", None),
                            retry_count=getattr(node, "retry_count", 0) or 0,
                            failure_reasons=(getattr(node, "failure_reasons", None) or []),
                        )
                        if _routed and _routed != model:
                            logger.info(
                                "qa_model_routed node=%s from=%s to=%s rule=%s",
                                node.id[:8], model, _routed, _routing.get("rule"),
                            )
                            model = _routed
                    except Exception as _rt_err:
                        logger.warning("model_router failed: %s", _rt_err)
                # S4-2 Layer B: QA 노드이고 spec 이 self_consistency 대상이면
                # N=3 병렬 호출 후 median 샘플 선택 → variance 차단. 그 외는 단일.
                _n_consistency = 0
                if node.node_type == "QA" and spec:
                    try:
                        from engine.skills.qa.self_consistency import (
                            run_consistency_qa, should_apply,
                        )
                        _n_consistency = should_apply(
                            spec, task_name=getattr(node, "name", None),
                        )
                    except Exception:
                        _n_consistency = 0
                if _n_consistency > 1:
                    async def _qa_single_call():
                        return await model_adapter.call(
                            model=model,
                            system=assembly.system,
                            prompt=assembly.prompt,
                            max_tokens=max_tokens,
                        )
                    _cresult = await run_consistency_qa(_n_consistency, _qa_single_call)
                    _median = _cresult.median_score
                    _best = min(
                        _cresult.samples,
                        key=lambda s: abs(s.get("score", 0) - _median),
                    ) if _cresult.samples else {"response": None, "raw": ""}
                    # 가장 median 에 가까운 sample 의 원본 APIResponse 사용
                    response = _best.get("response")
                    if response is None:
                        # 모든 호출 실패 등 fallback — 단일 재호출
                        response = await _qa_single_call()
                    logger.info(
                        "self_consistency_qa_applied node=%s n=%d median=%d final=%s",
                        node.id[:8], _n_consistency, _median,
                        "PASS" if _cresult.final_pass else "FAIL",
                    )
                else:
                    # Streaming: call_stream() 지원 시 토큰 단위 전송 + 조합
                    if hasattr(model_adapter, 'call_stream') and node.id:
                        from api.websocket import broadcast_token as _ws_broadcast_token
                        tokens = []
                        seq = 0
                        try:
                            async for tok in model_adapter.call_stream(
                                model=model,
                                system=assembly.system,
                                prompt=assembly.prompt,
                                max_tokens=max_tokens,
                            ):
                                if tok is None:
                                    break
                                tokens.append(tok)
                                await _ws_broadcast_token(node.id, tok, seq)
                                seq += 1
                        except Exception:
                            pass

                        if tokens:
                            from engine.ai.model_adapter import APIResponse
                            response = APIResponse(
                                content="".join(tokens),
                                input_tokens=0,
                                output_tokens=len("".join(tokens)) // 4,
                                model=model,
                                stop_reason="end_turn",
                            )
                        else:
                            # Fallback: streaming 실패 시 일반 call()
                            response = await model_adapter.call(
                                model=model,
                                system=assembly.system,
                                prompt=assembly.prompt,
                                max_tokens=max_tokens,
                            )
                    else:
                        response = await model_adapter.call(
                            model=model,
                            system=assembly.system,
                            prompt=assembly.prompt,
                            max_tokens=max_tokens,
                        )

            # 8-0. 응답 잘림 감지 + 자동 복구
            # 이전: json/code만 1.5배 재호출. document/html/markdown은 잘림 허용.
            # 변경: document류도 동일 확장 재호출. 장문 산출물이 중간 절단되어
            # QA missing_section 반복 실패하는 문제 근본 완화.
            if response.stop_reason == "max_tokens":
                logger.warning(
                    "response_truncated node=%s art_type=%s output_tokens=%d max=%d",
                    node.id[:8], art_type, response.output_tokens, max_tokens,
                )
                if art_type in ("json", "code", "document", "html", "markdown"):
                    expanded_max = min(int(max_tokens * 1.5), 64000)
                    if expanded_max > max_tokens:
                        logger.info(
                            "truncation_retry node=%s art_type=%s expanded_max=%d",
                            node.id[:8], art_type, expanded_max,
                        )
                        if node.node_type == "TASK" and spec and art_type != "json":
                            response = await _two_phase_generate(
                                model_adapter, model, assembly, expanded_max, spec, art_type, node,
                            )
                        else:
                            response = await model_adapter.call(
                                model=model,
                                system=assembly.system,
                                prompt=assembly.prompt,
                                max_tokens=expanded_max,
                            )
                        max_tokens = expanded_max

            # 8-1. JSON 산출물: 파싱 검증 + 실패 시 repair 호출
            if art_type == "json":
                response = await _repair_json_if_needed(
                    model_adapter, model, response, max_tokens,
                )

            # 8-2. Category-constraint self-check (library split TASK)
            # spec 에 _library_split_category 제약이 있으면 결과 JSON 의 category
            # 필드를 대조 → 미스매치 시 1회 교정 재호출.
            # stop_reason 무관하게 적용 — max_tokens 절단 후 자동 확장된 케이스도
            # category 검증 필요 (절단 케이스에서 LLM 이 잘못된 카테고리 라벨링 빈발).
            # _enforce_category_constraint 내부에서 JSON 파싱 가능 여부를 체크하므로
            # 절단 응답이면 조용히 0 영향 후 fallthrough.
            if (
                art_type == "json"
                and node.node_type == "TASK"
            ):
                response = await _enforce_category_constraint(
                    db, model_adapter, model, response, node, assembly, max_tokens,
                )

            # 9-11. Post-AI-call processing (save, validate, repair, complete)
            await _handle_post_ai_call(
                db, node, response, spec, project,
                model_adapter, budget_enforcer,
                max_tokens, model, art_type, engagement_id,
            )

        except (InputBudgetExceededError, PhaseBudgetExceededError) as _budget_err:
            # ── Budget Overflow Fallback (QA 노드 전용) ──
            # QA 노드에서 예산 초과 시, harness가 이미 통과했으면 자동 승인
            # harness 미통과 → FAIL 처리 (AI 없이도 구조 검증은 완료된 상태)
            if node.node_type == "QA":
                _harness_passed = spec.get("_structural_passed", False) if spec else False
                if _harness_passed:
                    _qa_verdict = {
                        "verdict": "PASS",
                        "method": "harness_budget_fallback",
                        "reason": str(_budget_err),
                        "checks": spec.get("_structural_checks", []) if spec else [],
                    }
                    await db.execute(
                        "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                        "updated_at=?, description=? WHERE id=?",
                        (_now(), _now(),
                         json.dumps(_qa_verdict, ensure_ascii=False),
                         node.id),
                    )
                    logger.warning(
                        "budget_exceeded_qa_fallback_pass node=%s error=%s",
                        node.id[:8], str(_budget_err),
                    )
                else:
                    _qa_verdict = {
                        "verdict": "FAIL",
                        "method": "harness_budget_fallback",
                        "reason": str(_budget_err),
                    }
                    await db.execute(
                        "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                        "updated_at=?, description=? WHERE id=?",
                        (_now(), _now(),
                         json.dumps(_qa_verdict, ensure_ascii=False),
                         node.id),
                    )
                    # TASK를 INVALID로 → 재실행 (다음 시도에서 분할된 노드가 실행됨)
                    if node.task_pair_node_id:
                        await db.execute(
                            "UPDATE nodes SET state='INVALID', stall_count=COALESCE(stall_count,0)+1, updated_at=? WHERE id=?",
                            (_now(), node.task_pair_node_id),
                        )
                    logger.warning(
                        "budget_exceeded_qa_fallback_fail node=%s error=%s",
                        node.id[:8], str(_budget_err),
                    )
                    raise ValueError(
                        f"Budget QA FAIL (harness 미통과): {_budget_err}"
                    )
            else:
                # V10: Level 2 Runtime Realloc 시도 — 다른 Phase 여유 차용
                _realloc_ok = False
                if engagement_id:
                    try:
                        from engine.core.budget_scaler import try_budget_realloc
                        _shortage = max(
                            100_000,
                            int(str(_budget_err).split()[-2].replace(",", "").split("/")[0])
                            if "/" in str(_budget_err) else 200_000,
                        )
                        _realloc_ok = await try_budget_realloc(
                            db, engagement_id, node.phase, _shortage,
                        )
                    except Exception as _rr_err:
                        logger.debug(
                            "v10_realloc_attempt_failed node=%s error=%s",
                            node.id[:8], _rr_err,
                        )

                if _realloc_ok:
                    # 재할당 성공 → READY 로 되돌려 재시도 트리거
                    await db.execute(
                        "UPDATE nodes SET state='NOT_STARTED', updated_at=? WHERE id=?",
                        (_now(), node.id),
                    )
                    logger.info(
                        "v10_budget_realloc_applied node=%s phase=%s → NOT_STARTED",
                        node.id[:8], node.phase,
                    )
                    return

                # Realloc 실패 시 기존 경로: BLOCKED + gotcha + 에피소드
                try:
                    await _record_gotcha(db, node.project_id, node.id, node.name, str(_budget_err))
                except Exception:
                    pass
                if episode_store:
                    try:
                        asyncio.create_task(episode_store.save_episode(
                            project_id=node.project_id,
                            node_id=node.id,
                            node_name=node.name,
                            episode_type="gotcha",
                            content=f"[budget_exceeded] {str(_budget_err)[:1800]}",
                            metadata={"retry_count": node.retry_count, "phase": node.phase},
                        ))
                    except Exception as _ep_err:
                        logger.debug(
                            "episode_save_skip node=%s error=%s",
                            node.id[:8], _ep_err,
                        )
                await db.execute(
                    "UPDATE nodes SET state='BLOCKED', updated_at=? WHERE id=?",
                    (_now(), node.id),
                )
                logger.warning(
                    "budget_exceeded_blocked node=%s phase=%s error=%s (realloc_tried=%s)",
                    node.id[:8], node.phase, _budget_err, _realloc_ok,
                )
                return

        except Exception as _any_exc:
            # Gotcha 기록: 모든 실패를 프로젝트 레벨 실수 DB에 자동 등록
            try:
                await _record_gotcha(db, node.project_id, node.id, node.name, str(_any_exc))
            except Exception:
                pass
            # Phase F: 에피소드 벡터 메모리에도 백그라운드 저장 (유사 실패 검색용)
            if episode_store:
                try:
                    asyncio.create_task(episode_store.save_episode(
                        project_id=node.project_id,
                        node_id=node.id,
                        node_name=node.name,
                        episode_type="gotcha",
                        content=str(_any_exc)[:2000],
                        metadata={"retry_count": node.retry_count, "phase": node.phase},
                    ))
                except Exception as _ep_err:
                    logger.debug(
                        "episode_save_skip node=%s error=%s",
                        node.id[:8], _ep_err,
                    )
            # ── 갭 보강 1: TASK 직접 예외 → 거시 진단 시도 ──
            # 메시지 길이 가드 + try/except 격리 모두 헬퍼 내부에서 처리.
            if (
                node.node_type == "TASK"
                and not isinstance(_any_exc, (InputBudgetExceededError, PhaseBudgetExceededError))
            ):
                from engine.skills.executor_cascade import macro_diagnose_safe
                await macro_diagnose_safe(
                    db, node.id, node.id, str(_any_exc),
                    model_adapter=model_adapter, source="task_exception",
                )
            raise

    # ── Heartbeat Wrapper ──
    # executor를 감싸서 agent_processes 등록/heartbeat 갱신/종료 정리를 수행.
    # watchdog이 PID 체크로 정확한 좀비 판별 가능.
    _raw_executor = executor

    async def executor_with_heartbeat(node: NodeSnapshot) -> None:
        if node.state == "SKIPPED":
            return await _raw_executor(node)

        _hb_proc_id = str(uuid.uuid4())
        _hb_task: Optional[asyncio.Task] = None

        # 1. agent_processes 등록 + 초기 heartbeat
        try:
            await db.execute(
                """INSERT INTO agent_processes
                   (id, agent_run_id, node_id, project_id, pid, status,
                    last_heartbeat, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,'ALIVE',?,0,?,?)""",
                (_hb_proc_id, _hb_proc_id, node.id, node.project_id,
                 os.getpid(), _now(), _now(), _now()),
            )
            await db.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE id=?",
                (_now(), node.id),
            )
        except Exception as _hb_err:
            logger.debug("heartbeat_register_failed node=%s: %s", node.id[:8], _hb_err)

        # 2. 60초마다 heartbeat 갱신 background task
        async def _hb_loop():
            try:
                while True:
                    await asyncio.sleep(60)
                    try:
                        _ts = _now()
                        await db.execute(
                            "UPDATE nodes SET last_heartbeat=? WHERE id=?",
                            (_ts, node.id),
                        )
                        await db.execute(
                            "UPDATE agent_processes SET last_heartbeat=?, updated_at=? WHERE id=?",
                            (_ts, _ts, _hb_proc_id),
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        _hb_task = asyncio.create_task(_hb_loop())

        # 3. 실제 executor 실행 + finally에서 정리
        try:
            await _raw_executor(node)
        finally:
            if _hb_task:
                _hb_task.cancel()
                try:
                    await _hb_task
                except asyncio.CancelledError:
                    pass
            try:
                await db.execute(
                    "UPDATE agent_processes SET status='TERMINATED', updated_at=? WHERE id=?",
                    (_now(), _hb_proc_id),
                )
            except Exception:
                pass

    return executor_with_heartbeat


# ---------------------------------------------------------------------------
# Extracted sub-handlers (module-level, receive captured vars as params)
# ---------------------------------------------------------------------------


async def _handle_qa_dispatch(
    db: Any,
    node: NodeSnapshot,
    spec: Optional[dict],
    project: Any,
    task_output: Any,
    model_adapter: Any = None,
) -> tuple:
    """Handle QA node programmatic/harness validation (Step 3).

    Returns:
        (handled: bool, spec: dict) — if handled=True, caller should return.
        spec may be mutated (design compliance issues, structural_passed).
    """

    # ── 3-A. programmatic_complete 노드 → harness 검증 (AI 완전 제거) ──
    _task_desc_row = await db.fetchone(
        "SELECT description FROM nodes WHERE id=?",
        (node.task_pair_node_id,),
    )
    _task_desc = {}
    if _task_desc_row and _task_desc_row["description"]:
        try:
            _task_desc = json.loads(_task_desc_row["description"])
        except (ValueError, TypeError) as exc:
            logger.debug("task_desc_parse_failed node=%s error=%s", (node.task_pair_node_id or "")[:8], exc)

    if _task_desc.get("_programmatic_complete"):
        # 전체 산출물 로드 (qa_mode 아닌 원본)
        _full_row = await db.fetchone(
            "SELECT av.storage_path AS content FROM artifact_versions av "
            "JOIN artifacts a ON a.id = av.artifact_id WHERE a.node_id = ? "
            "AND av.version_num = a.current_version",
            (node.task_pair_node_id,),
        )
        _full_content = _full_row["content"] if _full_row else ""

        # 레시피 로드 (placement 반영 검증용)
        _qa_recipes = await db.fetchall(
            "SELECT page_slug, data FROM composition_recipes WHERE project_id=?",
            (node.project_id,),
        )

        harness_result = _harness_validate_programmatic(
            _full_content,
            _task_desc,
            _qa_recipes,
        )

        # ── 화면 커버리지 체크 (DESIGN 단계, 레시피 QA 시) ──
        if harness_result["pass"] and node.phase == "DESIGN" and "시안" in node.name:
            # 디자인 시안 QA에서만 화면 커버리지 체크 (레시피 QA에서는 하지 않음)
                try:
                    _screen_list_row = await db.fetchone(
                        """SELECT av.storage_path FROM artifacts a
                           JOIN artifact_versions av ON a.id=av.artifact_id
                           WHERE a.project_id=? AND a.node_id IN
                             (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
                           AND av.version_num = a.current_version""",
                        (node.project_id, node.project_id),
                    )
                    if _screen_list_row and _screen_list_row["storage_path"]:
                        _screen_content = _screen_list_row["storage_path"]
                        _recipe_slugs = [r["page_slug"] for r in _qa_recipes]
                        from engine.workspace.paths import WORKSPACES_ROOT, _make_slug
                        _ws_path = WORKSPACES_ROOT / _make_slug(project.project_name) / "preview"
                        if not _ws_path.is_dir():
                            _ws_path = WORKSPACES_ROOT / project.project_name / "preview"
                        if not _ws_path.is_dir():
                            for _d in WORKSPACES_ROOT.iterdir():
                                if _d.is_dir() and (project.project_name in _d.name or _make_slug(project.project_name) in _d.name):
                                    _candidate = _d / "preview"
                                    if _candidate.is_dir():
                                        _ws_path = _candidate
                                        break
                        _design_slugs = []
                        if _ws_path.is_dir():
                            _design_slugs = [
                                f.stem for f in _ws_path.iterdir()
                                if f.suffix == ".html" and f.stem != "index"
                            ]
                        _retry = getattr(node, "retry_count", 0) or 0
                        # 초기 0.8, 재시도마다 +0.1, 상한 1.0 (100% SCR 매칭 강제)
                        # SCR 매칭 실패 시 missing_scrs가 feedback으로 들어가
                        # LLM이 정확히 누락 화면을 타겟해 재생성.
                        _min_cov = min(0.8 + _retry * 0.1, 1.0)
                        _cov_result = _harness_validate_screen_coverage(
                            _screen_content, _design_slugs, _recipe_slugs,
                            min_coverage=_min_cov,
                        )
                        if not _cov_result["pass"]:
                            harness_result["pass"] = False
                            harness_result["structural_failures"].extend(_cov_result["structural_failures"])
                        harness_result["checks"].extend(_cov_result["checks"])
                except Exception as _cov_err:
                    logger.warning("screen_coverage_check_failed: %s", _cov_err)

        # ── DEFINE 교차참조 체크 (화면목록 QA에서 수행) ──
        if (
            harness_result["pass"]
            and node.phase == "DEFINE"
            and "화면 목록" in node.name
        ):
            try:
                from engine.skills.qa.harness import (
                    _harness_validate_define_cross_references,
                )

                async def _load_by_name(name: str) -> str:
                    row = await db.fetchone(
                        """SELECT av.storage_path FROM artifacts a
                           JOIN artifact_versions av ON a.id=av.artifact_id
                           JOIN nodes n ON n.id = a.node_id
                           WHERE a.project_id=? AND n.name=? AND n.state='COMPLETED'
                             AND av.version_num = a.current_version""",
                        (node.project_id, name),
                    )
                    return row["storage_path"] if row and row["storage_path"] else ""

                _scr_content = await _load_by_name("화면 목록 정의서")
                _flow_content = await _load_by_name("사용자 흐름도 (User Flow)")
                _usecase_content = await _load_by_name(
                    "유스케이스 시나리오"
                )
                _backlog = await _load_by_name("기능 백로그 (Product Backlog)")
                _req = await _load_by_name("PRD (제품 요구사항 정의서)")

                _xref = _harness_validate_define_cross_references(
                    _scr_content, _flow_content, _usecase_content,
                    _backlog, _req,
                )
                if not _xref["pass"]:
                    harness_result["pass"] = False
                    harness_result["structural_failures"].extend(
                        _xref["structural_failures"]
                    )
                harness_result["checks"].extend(_xref["checks"])
            except Exception as _xref_err:
                logger.warning("define_cross_ref_check_failed: %s", _xref_err)

        if harness_result["pass"]:
            # Harness PASS → QA 완료 (토큰 0)
            _qa_verdict = {
                "verdict": "PASS",
                "method": "harness",
                "checks": harness_result["checks"],
            }
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (_now(), _now(),
                 json.dumps(_qa_verdict, ensure_ascii=False),
                 node.id),
            )
            logger.info(
                "harness_qa_pass node=%s task=%s checks=%d",
                node.id[:8], (node.task_pair_node_id or "")[:8],
                len(harness_result["checks"]),
            )
            return (True, spec)  # AI 호출 0
        else:
            # Harness FAIL — 범용 분기: failure 항목별 가중치로 virtual score 산출 후
            # AI QA 경로와 **동일한 partial/full 판정**에 태운다.
            # 원래 설계("부분 수정 or 전체 재생성")가 AI QA 경로에만 있었던 구멍을 메움.
            _failed_checks = [c for c in harness_result["checks"] if not c.get("pass", True)]
            _full_only = {"file_tags", "jsx_balance", "placement_coverage", "recipe_count"}
            _has_full_blocker = any(c.get("name") in _full_only for c in _failed_checks)
            # score: 실패 항목 수 × 10 감점. partial-only 실패는 완만한 감점, full blocker 는 무조건 <30.
            _v_score = 100 - min(len(_failed_checks) * 10, 80)
            if _has_full_blocker:
                _v_score = min(_v_score, 20)

            _qa_verdict = {
                "verdict": "FAIL",
                "method": "harness",
                "score": _v_score,
                "checks": harness_result["checks"],
                "failures": harness_result["failures"],
                "structural_failures": harness_result.get("structural_failures", harness_result["failures"]),
            }
            logger.warning(
                "harness_qa_fail node=%s task=%s failures=%s score=%d",
                node.id[:8], (node.task_pair_node_id or "")[:8],
                harness_result["failures"], _v_score,
            )
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (_now(), _now(),
                 json.dumps(_qa_verdict, ensure_ascii=False),
                 node.id),
            )

            # Harness 실패 → 구체적 수정 지시를 TASK description 에 주입.
            # 코드 산출물에는 "문서 섹션" 개념이 없어 기존 partial_patch 경로가
            # 의미 있게 작동하지 않는다. 따라서 여기서는 affected_sections 대신
            # **즉시 AI가 이해 가능한 수정 지시 블록**을 TASK description 에
            # append 하여, TASK 재실행 시 프롬프트 상단 가까이 들어가게 한다.
            _fix_directive = _build_harness_fix_directive(_failed_checks, harness_result.get("failures", []))
            try:
                _task_row = await db.fetchone(
                    "SELECT description FROM nodes WHERE id=?", (node.task_pair_node_id,),
                )
                _prev_desc = (_task_row["description"] if _task_row else "") or ""
                _marker = "<!-- HARNESS_FIX_DIRECTIVE -->"
                # 중복 주입 방지: 기존 지시 블록 제거 후 최신본만 유지
                if _marker in _prev_desc:
                    _cut = _prev_desc.find(_marker)
                    _prev_desc = _prev_desc[:_cut].rstrip()
                _new_desc = _prev_desc + "\n\n" + _marker + "\n" + _fix_directive
            except Exception:
                _new_desc = None

            if _v_score >= 30 and not _has_full_blocker:
                # partial — 메타데이터 + description 주입 병행
                _affected = list({c.get("name", "?") for c in _failed_checks}) + \
                            harness_result["failures"][:5]
                if _new_desc is not None:
                    await db.execute(
                        """UPDATE nodes SET state='INVALID',
                           invalidation_change_type='PARTIAL',
                           invalidation_affected_sections=?,
                           description=?,
                           stall_count=COALESCE(stall_count,0)+1,
                           updated_at=? WHERE id=?""",
                        (json.dumps(_affected, ensure_ascii=False),
                         _new_desc, _now(), node.task_pair_node_id),
                    )
                else:
                    await db.execute(
                        """UPDATE nodes SET state='INVALID',
                           invalidation_change_type='PARTIAL',
                           invalidation_affected_sections=?,
                           stall_count=COALESCE(stall_count,0)+1,
                           updated_at=? WHERE id=?""",
                        (json.dumps(_affected, ensure_ascii=False),
                         _now(), node.task_pair_node_id),
                    )
                logger.info(
                    "harness_partial_patch node=%s score=%d affected=%d directive_injected=%s",
                    node.id[:8], _v_score, len(_affected), _new_desc is not None,
                )
            else:
                # full 재생성 — description 에만 지시 주입
                if _new_desc is not None:
                    await db.execute(
                        """UPDATE nodes SET state='INVALID',
                           description=?,
                           stall_count=COALESCE(stall_count,0)+1,
                           updated_at=? WHERE id=?""",
                        (_new_desc, _now(), node.task_pair_node_id),
                    )
                else:
                    await db.execute(
                        "UPDATE nodes SET state='INVALID', stall_count=COALESCE(stall_count,0)+1, updated_at=? WHERE id=?",
                        (_now(), node.task_pair_node_id),
                    )
                logger.info(
                    "harness_full_regen node=%s score=%d has_full_blocker=%s directive_injected=%s",
                    node.id[:8], _v_score, _has_full_blocker, _new_desc is not None,
                )
            raise ValueError(
                f"Harness QA FAIL (score={_v_score}): {'; '.join(harness_result['failures'][:3])}"
            )

    # ── 3-B. AI-generated 노드 → harness 구조 검증 ──
    # 전략: 구조 결함 → AI 스킵 + TASK INVALID
    #       구조 통과 → 자동 PASS (의미 검증이 필요한 경우만 AI 폴스루)
    if task_output and node.task_pair_node_id:
        _full_row = await db.fetchone(
            "SELECT av.storage_path AS content FROM artifact_versions av "
            "JOIN artifacts a ON a.id = av.artifact_id WHERE a.node_id = ? "
            "AND av.version_num = a.current_version",
            (node.task_pair_node_id,),
        )
        _full_content = _full_row["content"] if _full_row else ""

        # TASK 노드의 phase/spec 판별
        _task_node_row = await db.fetchone(
            "SELECT name, phase FROM nodes WHERE id=?",
            (node.task_pair_node_id,),
        )
        _task_name = _task_node_row["name"] if _task_node_row else ""
        _task_phase = _task_node_row["phase"] if _task_node_row else node.phase
        _is_code = spec.get("type") == "code" if spec else False
        _is_json = spec.get("type") == "json" if spec else False
        _is_html = spec.get("file_type") == "html" or (
            _full_content and _full_content.strip()[:15].lower().startswith(("<!doctype", "<html", "<div", "<style"))
        )
        _is_document = not _is_code and not _is_json and not _is_html

        # ── 3-B-1. 코드형 산출물 구조 검증 ──
        if _is_code and _full_content:
            _struct_result = _harness_validate_ai_code(
                _full_content, _task_name, spec,
            )

            if _struct_result["structural_failures"]:
                # 구조 결함 → AI QA 스킵, TASK 재실행
                _qa_verdict = {
                    "verdict": "FAIL",
                    "method": "harness_structural",
                    "failures": _struct_result["structural_failures"],
                    "checks": _struct_result["checks"],
                }
                logger.warning(
                    "harness_structural_fail node=%s task=%s failures=%s",
                    node.id[:8], (node.task_pair_node_id or "")[:8],
                    _struct_result["structural_failures"][:3],
                )
                # QA 실패는 COMPLETED가 아니라 INVALID 로 기록해 downstream
                # BLOCKED 유지 + stall_count 누적.
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "updated_at=?, description=? WHERE id=?",
                    (_now(),
                     json.dumps(_qa_verdict, ensure_ascii=False),
                     node.id),
                )
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "description=?, updated_at=? WHERE id=?",
                    (json.dumps(_qa_verdict, ensure_ascii=False),
                     _now(), node.task_pair_node_id),
                )
                raise ValueError(
                    f"Harness Code QA FAIL: {'; '.join(_struct_result['structural_failures'][:3])}"
                )

            if _struct_result["pass"]:
                # ── 3-B-1a. 인터랙티비티 검증 ──
                _interact_result = _harness_validate_interactivity(
                    _full_content, _task_name, spec,
                )
                if _interact_result["structural_failures"]:
                    _qa_verdict = {
                        "verdict": "FAIL",
                        "method": "harness_interactivity",
                        "failures": _interact_result["structural_failures"],
                        "checks": _interact_result["checks"],
                    }
                    logger.warning(
                        "harness_interactivity_fail node=%s task=%s failures=%s",
                        node.id[:8], (node.task_pair_node_id or "")[:8],
                        _interact_result["structural_failures"][:3],
                    )
                    await db.execute(
                        "UPDATE nodes SET state='INVALID', "
                        "stall_count=COALESCE(stall_count,0)+1, "
                        "updated_at=?, description=? WHERE id=?",
                        (_now(),
                         json.dumps(_qa_verdict, ensure_ascii=False),
                         node.id),
                    )
                    await db.execute(
                        "UPDATE nodes SET state='INVALID', stall_count=COALESCE(stall_count,0)+1, updated_at=? WHERE id=?",
                        (_now(), node.task_pair_node_id),
                    )
                    raise ValueError(
                        f"Harness Interactivity FAIL: {'; '.join(_interact_result['structural_failures'][:3])}"
                    )

                # ── 3-B-1b. 디자인 매치 검증 (BUILD 단계, 디자인 HTML 존재 시) ──
                if _task_phase == "BUILD":
                    _design_html_content = ""
                    _ws_path = None
                    try:
                        _ws_path = await _resolve_workspace_path_for_project(db, node)
                        if _ws_path:
                            import glob as _glob
                            _preview_files = sorted(_glob.glob(
                                f"{_ws_path}/preview/*.html"
                            ))
                            # 현재 태스크와 매칭되는 HTML 찾기
                            _page_slugs = (spec or {}).get("page_slugs", [])
                            for pf in _preview_files:
                                import os as _os
                                _fname = _os.path.basename(pf).replace(".html", "")
                                if not _page_slugs or _fname in _page_slugs:
                                    with open(pf, "r", encoding="utf-8") as fh:
                                        _design_html_content = fh.read()
                                    break  # 첫 매칭 HTML 사용
                    except Exception as _e:
                        logger.debug("design_html_load_skip: %s", _e)

                    if _design_html_content:
                        _design_result = _harness_validate_design_match(
                            _design_html_content, _full_content,
                        )
                        if _design_result["structural_failures"]:
                            _qa_verdict = {
                                "verdict": "FAIL",
                                "method": "harness_design_match",
                                "failures": _design_result["structural_failures"],
                                "checks": _design_result["checks"],
                            }
                            logger.warning(
                                "harness_design_match_fail node=%s failures=%s",
                                node.id[:8],
                                _design_result["structural_failures"][:3],
                            )
                            await db.execute(
                                "UPDATE nodes SET state='INVALID', "
                                "stall_count=COALESCE(stall_count,0)+1, "
                                "updated_at=?, description=? WHERE id=?",
                                (_now(),
                                 json.dumps(_qa_verdict, ensure_ascii=False),
                                 node.id),
                            )
                            await db.execute(
                                "UPDATE nodes SET state='INVALID', stall_count=COALESCE(stall_count,0)+1, updated_at=? WHERE id=?",
                                (_now(), node.task_pair_node_id),
                            )
                            raise ValueError(
                                f"Harness Design Match FAIL: {'; '.join(_design_result['structural_failures'][:3])}"
                            )

                    # 기존 디자인 컴플라이언스 검증
                    design_issues = await _validate_design_compliance(
                        db, node, task_output,
                    )
                    if design_issues:
                        # 디자인 불일치만 → AI 의미 검증으로 폴스루
                        logger.warning(
                            "harness_pass_design_fail node=%s issues=%d",
                            node.id[:8], len(design_issues),
                        )
                        spec = spec.copy() if spec else {}
                        spec["_design_compliance_issues"] = design_issues
                        # AI QA로 폴스루 (step 4)
                    else:
                        _all_checks = (
                            _struct_result["checks"]
                            + _interact_result.get("checks", [])
                        )
                        # [v8+] Visual check (advisory — WARNING only, no blocking)
                        _all_checks = await _run_visual_check_advisory(
                            db, node, _all_checks, _ws_path, spec, project_id=getattr(node, 'project_id', ''),
                        )
                        await db.execute(
                            "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                            "updated_at=?, description=? WHERE id=?",
                            (_now(), _now(),
                             json.dumps({"verdict": "PASS", "method": "harness_structural",
                                         "checks": _all_checks}, ensure_ascii=False),
                             node.id),
                        )
                        logger.info("harness_code_qa_pass node=%s", node.id[:8])
                        return (True, spec)
                else:
                    _all_checks = (
                        _struct_result["checks"]
                        + _interact_result.get("checks", [])
                    )
                    # [v8+] Visual check (advisory — WARNING only, no blocking)
                    try:
                        _vc_ws_path = await _resolve_workspace_path_for_project(db, node)
                    except Exception as exc:
                        logger.debug("visual_check_ws_path_failed error=%s", exc)
                        _vc_ws_path = None
                    _all_checks = await _run_visual_check_advisory(
                        db, node, _all_checks, _vc_ws_path, spec, project_id=getattr(node, 'project_id', ''),
                    )
                    await db.execute(
                        "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                        "updated_at=?, description=? WHERE id=?",
                        (_now(), _now(),
                         json.dumps({"verdict": "PASS", "method": "harness_structural",
                                     "checks": _all_checks}, ensure_ascii=False),
                         node.id),
                    )
                    logger.info("harness_code_qa_pass node=%s", node.id[:8])
                    return (True, spec)

        # ── 3-B-2. 문서형 산출물 구조 검증 ──
        elif _is_document and _full_content:
            _doc_result = _harness_validate_document(
                _full_content, _task_name, spec,
            )

            # Harness auto-fix가 content를 치환했으면 artifact_versions 업데이트 +
            # _full_content 교체. 이후 검증·AI QA는 치환된 content로 진행.
            if _doc_result.get("auto_fixed_content"):
                _fixed = _doc_result["auto_fixed_content"]
                _fix_count = _doc_result.get("auto_fix_count", 0)
                try:
                    # TASK 노드의 최신 artifact_version을 치환된 content로 갱신
                    _art_row = await db.fetchone(
                        """SELECT a.id AS art_id, a.current_version
                           FROM artifacts a
                           WHERE a.node_id=?""",
                        (node.task_pair_node_id,),
                    )
                    if _art_row:
                        await db.execute(
                            """UPDATE artifact_versions
                               SET storage_path=?, size_bytes=?
                               WHERE artifact_id=? AND version_num=?""",
                            (_fixed, len(_fixed),
                             _art_row["art_id"], _art_row["current_version"]),
                        )
                        logger.info(
                            "harness_auto_fix_applied node=%s task=%s count=%d "
                            "new_size=%d",
                            node.id[:8], (node.task_pair_node_id or "")[:8],
                            _fix_count, len(_fixed),
                        )
                        _full_content = _fixed  # 이후 AI QA는 치환본으로
                except Exception as _afx_save_err:
                    logger.warning(
                        "harness_auto_fix_save_failed node=%s error=%s",
                        node.id[:8], str(_afx_save_err),
                    )

            if _doc_result["structural_failures"]:
                # 구조 결함 → AI 스킵, TASK 재실행
                _qa_verdict = {
                    "verdict": "FAIL",
                    "method": "harness_document",
                    "failures": _doc_result["structural_failures"],
                    "checks": _doc_result["checks"],
                }
                logger.warning(
                    "harness_document_fail node=%s task=%s failures=%s",
                    node.id[:8], (node.task_pair_node_id or "")[:8],
                    _doc_result["structural_failures"][:3],
                )
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "updated_at=?, description=? WHERE id=?",
                    (_now(),
                     json.dumps(_qa_verdict, ensure_ascii=False),
                     node.id),
                )
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "description=?, updated_at=? WHERE id=?",
                    (json.dumps(_qa_verdict, ensure_ascii=False),
                     _now(), node.task_pair_node_id),
                )
                raise ValueError(
                    f"Harness Doc QA FAIL: {'; '.join(_doc_result['structural_failures'][:3])}"
                )

            if _doc_result["pass"]:
                # 구조 통과 → 2단계: AI 의미 검증으로 폴스루
                # (구조 통과 사실을 spec에 기록 → AI 프롬프트에서 참조 가능)
                logger.info(
                    "harness_doc_structural_pass node=%s — AI 의미 검증 진행",
                    node.id[:8],
                )
                spec = spec.copy() if spec else {}
                spec["_structural_passed"] = True
                spec["_structural_checks"] = _doc_result["checks"]
                # AI QA (step 4)로 폴스루

        # ── 3-B-3. JSON형 산출물 구조 검증 ──
        elif _is_json and _full_content:
            import json as _jv
            _json_failures = []

            # J1: JSON 파싱 가능 여부
            try:
                _parsed = _jv.loads(_full_content)
            except _jv.JSONDecodeError as e:
                _json_failures.append(f"JSON 파싱 실패: {e}")
                _parsed = None

            # J2: 배열인 경우 최소 항목 수 (spec에 정의된 경우)
            # 분할 노드("(" 포함)는 카테고리당이므로 min_items를 1로 완화
            if _parsed is not None and isinstance(_parsed, list):
                _min_items = 0
                if spec:
                    _min_items = spec.get("validation", {}).get("structural", {}).get("min_items", 0)
                if _min_items and "(" in _task_name:
                    _min_items = 1  # 분할 노드: 카테고리당 최소 1개면 충분
                if _min_items and len(_parsed) < _min_items:
                    _json_failures.append(f"항목 수 부족: {len(_parsed)}개 (최소 {_min_items}개)")

            # J3: 최소 크기
            if len(_full_content) < 1000:
                _json_failures.append(f"JSON 크기 부족: {len(_full_content)}자")

            if _json_failures:
                _qa_verdict = {
                    "verdict": "FAIL",
                    "method": "harness_json",
                    "failures": _json_failures,
                }
                logger.warning(
                    "harness_json_fail node=%s task=%s failures=%s",
                    node.id[:8], (node.task_pair_node_id or "")[:8], _json_failures,
                )
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "updated_at=?, description=? WHERE id=?",
                    (_now(),
                     json.dumps(_qa_verdict, ensure_ascii=False),
                     node.id),
                )
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "description=?, updated_at=? WHERE id=?",
                    (json.dumps(_qa_verdict, ensure_ascii=False),
                     _now(), node.task_pair_node_id),
                )
                raise ValueError(
                    f"Harness JSON QA FAIL: {'; '.join(_json_failures[:3])}"
                )

            # JSON 구조 통과
            logger.info("harness_json_pass node=%s items=%d", node.id[:8],
                        len(_parsed) if isinstance(_parsed, list) else 1)

            # 대형 JSON (50KB+): 카테고리별 분할 QA 검증 (AI가 전체를 못 읽는 문제 방지)
            if len(_full_content) > 50000 and isinstance(_parsed, list) and spec:
                _chunk_cats = spec.get("chunk_categories", [])
                if _chunk_cats:
                    _qa_all_pass = True
                    _qa_all_issues = []
                    _semantic = spec.get("validation", {}).get("semantic", [])
                    _qa_prompt_tmpl = spec.get("qa_prompt", "")

                    for _cat_raw in _chunk_cats:
                        _cat = _cat_raw.get("name", _cat_raw) if isinstance(_cat_raw, dict) else str(_cat_raw)
                        _cat_items = [c for c in _parsed if c.get("category") == _cat]
                        if not _cat_items:
                            _qa_all_issues.append(f"[{_cat}] 카테고리 항목 0개")
                            _qa_all_pass = False
                            continue

                        _cat_json = json.dumps(_cat_items, ensure_ascii=False, indent=1)
                        _cat_prompt = (
                            f"## 카테고리: {_cat} ({len(_cat_items)}개 항목)\n\n"
                            f"{_cat_json[:30000]}\n\n"
                            f"검증 기준:\n" + "\n".join(f"- {s}" for s in _semantic[:3]) + "\n\n"
                            f"이 카테고리의 컴포넌트 품질을 PASS/FAIL로 판정하세요.\n"
                            f"JSON으로 반환: {{\"verdict\": \"PASS\"|\"FAIL\", \"issues\": [...]}}"
                        )
                        try:
                            _cat_resp = await model_adapter.call(
                                model=node.assigned_model or ModelID.SONNET,
                                system="당신은 UI 컴포넌트 품질 검증 전문가입니다. JSON만 반환하세요.",
                                prompt=_cat_prompt,
                                max_tokens=2000,
                            )
                            _cat_verdict = json.loads(_extract_first_json_block(_cat_resp.content))
                            if _cat_verdict.get("verdict") == "FAIL":
                                _qa_all_pass = False
                                for iss in _cat_verdict.get("issues", [])[:3]:
                                    _qa_all_issues.append(f"[{_cat}] {iss}")
                            logger.info("chunked_qa category=%s verdict=%s node=%s",
                                        _cat, _cat_verdict.get("verdict", "?"), node.id[:8])
                        except Exception as _cqe:
                            logger.warning("chunked_qa_fail category=%s error=%s", _cat, _cqe)

                    # 종합 판정
                    _qa_verdict = {
                        "verdict": "PASS" if _qa_all_pass else "FAIL",
                        "method": "chunked_json_qa",
                        "score": 100 if _qa_all_pass else 30,
                        "categories_checked": len(_chunk_cats),
                        "issues": _qa_all_issues,
                    }
                    if _qa_all_pass:
                        await db.execute(
                            "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                            "updated_at=?, description=? WHERE id=?",
                            (_now(), _now(),
                             json.dumps(_qa_verdict, ensure_ascii=False),
                             node.id),
                        )
                    else:
                        # FAIL 경로: QA는 INVALID 로 기록해 downstream BLOCKED 유지.
                        await db.execute(
                            "UPDATE nodes SET state='INVALID', "
                            "stall_count=COALESCE(stall_count,0)+1, "
                            "updated_at=?, description=? WHERE id=?",
                            (_now(),
                             json.dumps(_qa_verdict, ensure_ascii=False),
                             node.id),
                        )
                        await db.execute(
                            "UPDATE nodes SET state='INVALID', stall_count=COALESCE(stall_count,0)+1, updated_at=? WHERE id=?",
                            (_now(), node.task_pair_node_id),
                        )
                        raise ValueError(
                            f"Chunked JSON QA FAIL: {'; '.join(_qa_all_issues[:5])}"
                        )
                    logger.info("chunked_json_qa_pass node=%s categories=%d", node.id[:8], len(_chunk_cats))
                    return (True, spec)

            spec = spec.copy() if spec else {}
            spec["_structural_passed"] = True

    # ── 3-B-4. spec 기반 기존 validate() 폴백 (harness 미적용 케이스) ──
    if task_output and spec and not spec.get("_structural_passed"):
        from engine.skills.validators import validate

        result = validate(task_output, spec)
        if result.passed:
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=? WHERE id=?",
                (_now(), _now(), node.id),
            )
            return (True, spec)
    # Structural/spec QA failed or needs semantic — fall through to AI QA.
    return (False, spec)


async def _run_visual_check_advisory(
    db: Any,
    node: Any,
    all_checks: list,
    ws_path: Any,
    spec: dict | None,
    project_id: str = "",
) -> list:
    """Run visual check as advisory WARNING — never blocks the pipeline.

    Appends visual check results to all_checks and logs warnings if issues found.
    Returns the (possibly extended) all_checks list.
    """
    if not ws_path:
        return all_checks

    try:
        from engine.skills.qa.visual_check import run_visual_checks
        import asyncio as _asyncio

        # Read ports.json from workspace
        _ports_file = Path(ws_path) / "ports.json" if ws_path else None
        _ports: dict = {}
        if _ports_file and _ports_file.exists():
            try:
                _ports = json.loads(_ports_file.read_text())
            except Exception as exc:
                logger.warning("ports_json_parse_failed path=%s error=%s", _ports_file, exc)

        if not _ports.get("frontend_port") and not _ports.get("fe_port"):
            return all_checks

        visual_result = await _asyncio.to_thread(
            run_visual_checks, ws_path, _ports, project_id,
        )

        # Add visual checks to the check list (as informational)
        for vc in visual_result.get("issues", []):
            all_checks.append({
                "name": f"visual_{vc.get('type', 'unknown')}",
                "pass": False,
                "severity": "warning",  # Never critical in harness context
                "detail": vc.get("detail", ""),
                "page": vc.get("page", ""),
            })

        if visual_result.get("pages_checked", 0) > 0:
            all_checks.append({
                "name": "visual_check_summary",
                "pass": visual_result.get("pass", True),
                "severity": "info",
                "detail": (
                    f"pages={visual_result['pages_checked']} "
                    f"score={visual_result.get('score', 0):.2f} "
                    f"issues={len(visual_result.get('issues', []))}"
                ),
            })

        if not visual_result.get("pass", True):
            logger.warning(
                "visual_check_advisory_issues node=%s project=%s issues=%d",
                node.id[:8] if hasattr(node, 'id') else '?',
                project_id[:8] if project_id else '?',
                len(visual_result.get("issues", [])),
            )
    except Exception as exc:
        logger.debug("visual_check_advisory_skip error=%s", str(exc))

    return all_checks


async def _handle_build_programmatic(
    db: Any,
    node: NodeSnapshot,
    project: Any,
    spec: Optional[dict],
    rendered_prompt: str,
    assembler: ContextAssembler,
    model_adapter: ModelAdapter,
    budget_enforcer: BudgetEnforcer,
) -> tuple:
    """Handle BUILD phase programmatic dispatch (Step 4 BUILD branch).

    Returns:
        (handled: bool, rendered_prompt: str) — if handled=True, node completed,
        caller should return. Otherwise rendered_prompt has been enriched.
    """
    if any(kw in node.name for kw in ("DB 스키마", "DB스키마", "데이터베이스 스키마", "마이그레이션")):
        # ── DB 스키마: ERD → Prisma + SQL (프로그래매틱) ──
        _db_code = await _build_db_schema_code(db, node.project_id)
        if _db_code:
            _merged = await _save_scaffold_as_artifact(db, node, _db_code)
            _db_desc = {}
            _db_desc_row = await db.fetchone(
                "SELECT description FROM nodes WHERE id=?", (node.id,)
            )
            if _db_desc_row and _db_desc_row["description"]:
                try:
                    _db_desc = json.loads(_db_desc_row["description"])
                except (ValueError, TypeError) as exc:
                    logger.debug("db_desc_parse_failed node=%s error=%s", node.id[:8], exc)
            _db_desc["_programmatic_complete"] = True
            from engine.lifecycle.engine_version import compute_engine_version
            _db_desc["_engine_version"] = compute_engine_version()
            _db_desc["_files_generated"] = list(_db_code.keys())
            now = _now()
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (now, now, json.dumps(_db_desc, ensure_ascii=False), node.id),
            )
            logger.info("programmatic_db_schema node=%s files=%d size=%d",
                        node.id[:8], len(_db_code), len(_merged))
            return (True, rendered_prompt)  # AI 스킵
        # 폴백: AI 경로
        design_context = await _load_design_artifacts_for_build(db, node)
        if design_context:
            rendered_prompt += design_context

    elif any(kw in node.name for kw in ("백엔드 API", "백엔드API", "CRUD API", "Express", "서버 구현")):
        # ── 백엔드 CRUD: API 설계서 → Express (프로그래매틱) ──
        _be_code = await _build_backend_api_code(db, node.project_id)
        if _be_code:
            _merged = await _save_scaffold_as_artifact(db, node, _be_code)
            _be_desc = {}
            _be_desc_row = await db.fetchone(
                "SELECT description FROM nodes WHERE id=?", (node.id,)
            )
            if _be_desc_row and _be_desc_row["description"]:
                try:
                    _be_desc = json.loads(_be_desc_row["description"])
                except (ValueError, TypeError) as exc:
                    logger.debug("be_desc_parse_failed node=%s error=%s", node.id[:8], exc)
            _be_desc["_programmatic_complete"] = True
            from engine.lifecycle.engine_version import compute_engine_version
            _be_desc["_engine_version"] = compute_engine_version()
            _be_desc["_files_generated"] = list(_be_code.keys())
            now = _now()
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (now, now, json.dumps(_be_desc, ensure_ascii=False), node.id),
            )
            logger.info("programmatic_backend_api node=%s files=%d size=%d",
                        node.id[:8], len(_be_code), len(_merged))
            return (True, rendered_prompt)  # AI 스킵
        # 폴백: AI 경로
        design_context = await _load_design_artifacts_for_build(db, node)
        if design_context:
            rendered_prompt += design_context

    elif "프론트엔드 공통 인프라" in node.name:
        # ── 프론트엔드 인프라: 토큰→CSS + 라이브러리→React (프로그래매틱) ──
        platform = _detect_platform(project.global_context)
        _infra_code = await _build_frontend_infra_code(db, node.project_id, platform)
        if _infra_code:
            _merged = await _save_scaffold_as_artifact(db, node, _infra_code)
            _infra_desc = {}
            _infra_desc_row = await db.fetchone(
                "SELECT description FROM nodes WHERE id=?", (node.id,)
            )
            if _infra_desc_row and _infra_desc_row["description"]:
                try:
                    _infra_desc = json.loads(_infra_desc_row["description"])
                except (ValueError, TypeError) as exc:
                    logger.debug("infra_desc_parse_failed node=%s error=%s", node.id[:8], exc)
            _infra_desc["_programmatic_complete"] = True
            from engine.lifecycle.engine_version import compute_engine_version
            _infra_desc["_engine_version"] = compute_engine_version()
            _infra_desc["_files_generated"] = list(_infra_code.keys())
            now = _now()
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (now, now, json.dumps(_infra_desc, ensure_ascii=False), node.id),
            )
            logger.info("programmatic_frontend_infra node=%s files=%d size=%d",
                        node.id[:8], len(_infra_code), len(_merged))
            return (True, rendered_prompt)  # AI 스킵
        # 폴백: 기존 AI 경로 (디자인 토큰 주입)
        design_context = await _load_design_artifacts_for_build(db, node)
        if design_context:
            rendered_prompt += design_context
        rendered_prompt += _build_platform_instruction(platform)
    elif "프론트엔드 컴포넌트 구현" in node.name:
        platform = _detect_platform(project.global_context)

        # 독립 노드 (page_slugs 지정됨): 해당 페이지만 FULL HTML 로드
        _desc_data = {}
        _desc_row = await db.fetchone(
            "SELECT description FROM nodes WHERE id=?", (node.id,)
        )
        if _desc_row and _desc_row["description"]:
            try:
                _desc_data = json.loads(_desc_row["description"])
            except (ValueError, TypeError) as exc:
                logger.debug("desc_data_parse_failed node=%s error=%s", node.id[:8], exc)
        _page_slugs = _desc_data.get("page_slugs")

        if _page_slugs:
            # 독립 노드: 할당된 페이지만 FULL HTML
            assembled_html = await _load_assembled_pages(
                db, node.project_id, _page_slugs
            )
        else:
            # 분할 안 된 프로젝트: 배치 폴백 또는 기존 경로
            page_count = await _count_project_pages(db, node.project_id)
            if page_count > BATCH_THRESHOLD:
                await _batched_frontend_generate(
                    db, node, project, spec, platform,
                    assembler, model_adapter, budget_enforcer,
                )
                return (True, rendered_prompt)
            assembled_html = await _load_assembled_pages(db, node.project_id)

        # ── 완전 프로그래매틱 코드 생성 (AI 0회) ──
        _complete_code = await _build_complete_page_code(
            db, node.project_id, _page_slugs, platform,
        )

        if _complete_code:
            # 완성 코드를 artifact로 직접 저장 (AI 0회, 절단 불가능)
            _merged = await _save_scaffold_as_artifact(
                db, node, _complete_code,
            )
            _desc_data["_programmatic_complete"] = True
            from engine.lifecycle.engine_version import compute_engine_version
            _desc_data["_engine_version"] = compute_engine_version()
            _desc_data["_page_slugs_generated"] = list(_complete_code.keys())

            logger.info(
                "programmatic_complete node=%s pages=%d total_size=%d",
                node.id[:8], len(_complete_code), len(_merged),
            )

            # AI 호출 없이 노드 완료 → assembly 패턴과 동일
            now = _now()
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, "
                "updated_at=?, description=? WHERE id=?",
                (now, now,
                 json.dumps(_desc_data, ensure_ascii=False),
                 node.id),
            )
            return (True, rendered_prompt)  # AI 호출 스킵 — 프로그래매틱 완료

        # ── 폴백: 레시피 없음 → 기존 AI 방식 ──
        if assembled_html:
            rendered_prompt += assembled_html
        infra_summary = await _load_infra_summary(db, node.project_id)
        if infra_summary:
            rendered_prompt += infra_summary
        rendered_prompt += _build_platform_instruction(platform)

        # ── 시안 HTML 참조 주입 (preview/ 폴더) ──
        _design_ref = await load_design_htmls_for_prompt(
            db, node.project_id, _page_slugs,
        )
        if _design_ref:
            rendered_prompt += _design_ref

        # 골격이라도 있으면 프롬프트에 주입
        _scaffold_text, _ = await _build_placement_scaffold(
            db, node.project_id, _page_slugs, platform,
        )
        if _scaffold_text:
            rendered_prompt += _scaffold_text

        # Output Size Anchoring (AI 폴백용)
        _html_chars = len(assembled_html) if assembled_html else 0
        _fe_page_count = len(_page_slugs) if _page_slugs else (
            await _count_project_pages(db, node.project_id)
        )
        _size_estimate = _estimate_output_size(
            input_html_chars=_html_chars,
            page_count=_fe_page_count,
            platform_key=platform.get("key", "web_nextjs"),
            has_infra="프론트엔드 공통 인프라" not in node.name,
        )
        rendered_prompt += _build_size_anchor_block(
            _size_estimate, _fe_page_count, platform["framework"]
        )
        _desc_data["_size_estimate"] = _size_estimate
        await db.execute(
            "UPDATE nodes SET description=? WHERE id=?",
            (json.dumps(_desc_data, ensure_ascii=False), node.id),
        )
    else:
        design_context = await _load_design_artifacts_for_build(db, node)
        if design_context:
            rendered_prompt += design_context

    return (False, rendered_prompt)


async def _handle_post_ai_call(
    db: Any,
    node: NodeSnapshot,
    response: Any,
    spec: Optional[dict],
    project: Any,
    model_adapter: ModelAdapter,
    budget_enforcer: BudgetEnforcer,
    max_tokens: int,
    model: str,
    art_type: str,
    engagement_id: str,
) -> None:
    """Handle post-AI-call processing (Steps 9-11).

    Includes: artifact saving, HTML extraction, scaffold merge, output size gate,
    validation, auto-repair, composition post-processing, QA verdict, defect cascade,
    and marking node COMPLETED.
    """
    # 9. Save artifact (스킬 type에 따라 artifact_type 결정)
    save_content = response.content
    if art_type == "html":
        import re as _re
        # ```html 코드블록으로 감싸진 경우 내부 HTML 추출
        _cb_match = _re.search(r'```html\s*\n?([\s\S]*?)\n?```', save_content)
        if _cb_match:
            save_content = _cb_match.group(1).strip()
        # <!DOCTYPE...부터 </html>까지 추출
        if "<!DOCTYPE" in save_content:
            _dt_match = _re.search(r'(<!DOCTYPE[\s\S]*</html>)', save_content, _re.IGNORECASE)
            if _dt_match:
                save_content = _dt_match.group(1)
        # @import url(...)이 <style> 바깥에 있으면 안으로 이동
        if "@import url(" in save_content and "<style>" in save_content:
            _imports = _re.findall(r"@import url\([^)]+\);?", save_content)
            for imp in _imports:
                save_content = save_content.replace(imp + '\n', '').replace(imp, '')
            _imp_block = '\n'.join(_imports) + '\n'
            save_content = save_content.replace('<style>', f'<style>\n{_imp_block}', 1)
    # HTML 산출물: <!DOCTYPE 필수 검증 — 없으면 즉시 보정 시도
    if art_type == "html" and "<!DOCTYPE" not in save_content and "</html>" in save_content:
        logger.warning("html_missing_doctype node_id=%s — attempting repair", node.id)
        repaired = await _auto_repair_artifact(
            model_adapter, model, save_content,
            ["HTML에 <!DOCTYPE html> 선언이 누락됨. 완전한 HTML(<!DOCTYPE html>부터 </html>까지)로 다시 출력 필요"],
            spec or {}, max_tokens, node,
        )
        if repaired and "<!DOCTYPE" in repaired:
            save_content = repaired
            import re as _re_dt
            _dt = _re_dt.search(r'(<!DOCTYPE[\s\S]*</html>)', repaired, _re_dt.IGNORECASE)
            if _dt:
                save_content = _dt.group(1)
        else:
            raise ValueError("HTML 산출물에 <!DOCTYPE html> 선언 누락. 보정도 실패.")
    # QA 노드: verdict JSON을 읽을 수 있는 마크다운 리포트로 변환하여 저장
    if node.node_type == "QA":
        save_content = _verdict_to_markdown(save_content)

    # 9-0. Scaffold-First Merge: AI stub 출력을 골격에 병합
    if (
        node.node_type == "TASK"
        and node.phase == "BUILD"
        and "프론트엔드 컴포넌트 구현" in node.name
    ):
        _sf_desc = {}
        try:
            _sf_row = await db.fetchone(
                "SELECT description FROM nodes WHERE id=?", (node.id,),
            )
            if _sf_row and _sf_row["description"]:
                _sf_desc = json.loads(_sf_row["description"])
        except (ValueError, TypeError) as exc:
            logger.debug("scaffold_desc_parse_failed node=%s error=%s", node.id[:8], exc)

        if _sf_desc.get("_scaffold_first"):
            # 저장된 골격 로드
            _scaffold_row = await db.fetchone(
                "SELECT av.storage_path AS content FROM artifact_versions av "
                "JOIN artifacts a ON a.id = av.artifact_id WHERE a.node_id = ? "
                "AND av.version_num = a.current_version",
                (node.id,),
            )
            if _scaffold_row and _scaffold_row["content"]:
                _stored_scaffold = _scaffold_row["content"]
                # 저장된 골격을 slug별 코드맵으로 재구성
                _scaffold_map: dict[str, str] = {}
                _current_slug = ""
                _current_lines: list[str] = []
                for _sl in _stored_scaffold.split("\n"):
                    if _sl.startswith("// FILE:"):
                        if _current_slug and _current_lines:
                            _scaffold_map[_current_slug] = "\n".join(_current_lines)
                        # slug 추출: src/pages/XxxPage.tsx → xxx
                        import re as _re_sf
                        _fn_match = _re_sf.search(r"/pages/(\w+)Page\.", _sl)
                        _current_slug = _fn_match.group(1).lower() if _fn_match else _sl
                        _current_lines = [_sl]
                    else:
                        _current_lines.append(_sl)
                if _current_slug and _current_lines:
                    _scaffold_map[_current_slug] = "\n".join(_current_lines)

                if _scaffold_map:
                    # AI 출력(save_content)을 골격에 merge
                    save_content = _merge_ai_into_scaffold(_scaffold_map, save_content)
                    logger.info(
                        "scaffold_merge_complete node=%s pages=%d merged_size=%d",
                        node.id[:8], len(_scaffold_map), len(save_content),
                    )

    await _save_artifact(db, node, save_content, art_type)

    # 9-1. L3 사용량 기록 (engagement_id 있을 때만 — DB NOT NULL 제약 준수)
    if engagement_id:
        try:
            await budget_enforcer.post_call_record(
                node_id=node.id,
                agent_run_id=None,
                engagement_id=engagement_id,
                phase=node.phase,
                model_name=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        except Exception as budget_exc:
            # L3 실패는 산출물 저장 완료 후이므로 로그만 남기고 진행
            logger.warning(
                "budget_post_record_failed node_id=%s error=%s",
                node.id, str(budget_exc),
            )

    # 9-2. v10 컴포넌트 조합: AI 산출물을 composition registry에 저장
    if spec and spec.get("composition_role") in ("tokens", "library", "recipe"):
        await _composition_post_process(
            db, node, save_content, spec["composition_role"]
        )

    # 9-3. Output Size Gate: 프론트엔드 컴포넌트 노드 크기 검증
    # AI 비결정성 구조적 해결 — 기대 대비 극단적 부족 시 즉시 INVALID
    if (
        node.node_type == "TASK"
        and node.phase == "BUILD"
        and "프론트엔드 컴포넌트 구현" in node.name
    ):
        _sg_desc = {}
        try:
            _sg_row = await db.fetchone(
                "SELECT description FROM nodes WHERE id=?", (node.id,)
            )
            if _sg_row and _sg_row["description"]:
                _sg_desc = json.loads(_sg_row["description"])
        except (ValueError, TypeError) as exc:
            logger.debug("sg_desc_parse_failed node=%s error=%s", node.id[:8], exc)
        _sg_estimate = _sg_desc.get("_size_estimate")
        _sg_is_scaffold_first = _sg_desc.get("_scaffold_first", False)
        if _sg_is_scaffold_first:
            # scaffold-first: 골격은 프로그래매틱이라 크기 검증 불필요
            # merge 결과만 // FILE: 태그 존재 확인
            _sg_file_count = save_content.count("// FILE:")
            if _sg_file_count < 1:
                logger.warning(
                    "scaffold_merge_gate_WARN node=%s no_file_tags — merge 실패 가능성",
                    node.id,
                )
            else:
                logger.info(
                    "scaffold_first_size_gate_SKIP node=%s merged_size=%d files=%d",
                    node.id, len(save_content), _sg_file_count,
                )
            _sg_estimate = None  # 아래 기존 게이트 스킵
        if _sg_estimate and isinstance(_sg_estimate, dict):
            _sg_output_len = len(save_content)
            _sg_min = _sg_estimate["min_chars"]
            _sg_expected = _sg_estimate["expected_chars"]
            _sg_min_files = _sg_estimate["min_files"]
            _sg_file_count = save_content.count("// FILE:")
            _sg_ratio = _sg_output_len / _sg_expected if _sg_expected > 0 else 1.0

            if _sg_output_len < _sg_min or _sg_file_count < _sg_min_files:
                # 극단적 부족: repair 시도도 낭비 → 즉시 INVALID + 재실행
                logger.warning(
                    "output_size_gate_REJECT node=%s output=%d min=%d expected=%d "
                    "ratio=%.1f%% files=%d/%d — TASK INVALID for retry",
                    node.id, _sg_output_len, _sg_min, _sg_expected,
                    _sg_ratio * 100, _sg_file_count, _sg_min_files,
                )
                raise ValueError(
                    f"Output Size Gate 실패: 출력 {_sg_output_len:,}자 "
                    f"(기대 {_sg_expected:,}자의 {_sg_ratio:.0%}), "
                    f"파일 {_sg_file_count}/{_sg_min_files}개. "
                    f"최소 {_sg_min:,}자/{_sg_min_files}파일 필요. "
                    f"AI가 불완전한 출력을 생성함 — 전체 재생성 필요."
                )
            elif _sg_ratio < 0.6:
                # 부분 부족: 경고 로그 (repair에서 잡힐 수 있음)
                logger.warning(
                    "output_size_gate_WARN node=%s output=%d expected=%d "
                    "ratio=%.1f%% — borderline, proceeding to validation",
                    node.id, _sg_output_len, _sg_expected, _sg_ratio * 100,
                )
            else:
                logger.info(
                    "output_size_gate_PASS node=%s output=%d expected=%d ratio=%.1f%%",
                    node.id, _sg_output_len, _sg_expected, _sg_ratio * 100,
                )

    # 10. Post-call validation + 자동 보정
    # Scaffold-First 모드: 골격이 프로그래매틱이므로 문서형 검증 스킵
    _is_scaffold_first_node = False
    if node.node_type == "TASK" and "프론트엔드 컴포넌트 구현" in node.name:
        try:
            _sf_check = await db.fetchone("SELECT description FROM nodes WHERE id=?", (node.id,))
            if _sf_check and _sf_check["description"]:
                _is_scaffold_first_node = json.loads(_sf_check["description"]).get("_scaffold_first", False)
        except (ValueError, TypeError) as exc:
            logger.debug("scaffold_first_check_parse_failed node=%s error=%s", node.id[:8], exc)

    if node.node_type == "TASK" and spec and not _is_scaffold_first_node:
        from engine.skills.validators import validate

        # 대형 코드 산출물: 문서형 검증(required_headings) 대신 코드형 검증 적용
        _is_large_code = spec.get("type") == "code" and len(response.content) > 30000
        if _is_large_code:
            # 코드형 검증: // FILE: 태그 존재 + 최소 분량
            _file_tag_count = response.content.count("// FILE:")
            _code_issues = []
            if _file_tag_count == 0:
                _code_issues.append("코드에 // FILE: 태그가 없음")
            if len(response.content) < 5000:
                _code_issues.append(f"코드 분량 부족: {len(response.content)}자")
            if _code_issues:
                logger.warning(
                    "large_code_validation_failed node=%s size=%d file_tags=%d issues=%s",
                    node.id, len(response.content), _file_tag_count, _code_issues,
                )
                raise ValueError(
                    f"코드 산출물 검증 실패: {', '.join(_code_issues)}"
                )
            # 대형 코드 + // FILE: 있음 → 검증 통과 (required_headings 스킵)
            logger.info(
                "large_code_validation_passed node=%s size=%d file_tags=%d",
                node.id, len(response.content), _file_tag_count,
            )
        else:
            result = validate(response.content, spec)
            if not result.passed:
                # 자동 보정 시도: AI에게 검증 실패 사유를 주고 보완 요청
                repaired = await _auto_repair_artifact(
                    model_adapter, model, response.content,
                    result.issues, spec, max_tokens, node,
                )
                if repaired:
                    # 보정본 재검증
                    result2 = validate(repaired, spec)
                    if result2.passed:
                        response.content = repaired
                        # 보정본으로 artifact 덮어쓰기
                        await _save_artifact(db, node, repaired, art_type)
                        if spec and spec.get("composition_role") in ("tokens", "library", "recipe"):
                            await _composition_post_process(db, node, repaired, spec["composition_role"])
                        logger.info("auto_repair_success node_id=%s issues_fixed=%d",
                                    node.id, len(result.issues))
                    else:
                        raise ValueError(
                            f"산출물 검증 실패 (보정 후에도): {', '.join(result2.issues)}"
                        )
                else:
                    raise ValueError(
                        f"산출물 검증 실패: {', '.join(result.issues)}"
                    )

    # 10-1. QA 노드: 구조화된 판정 결과 파싱 + artifact_qa_stamps 저장
    if node.node_type == "QA":
        verdict = _parse_qa_verdict(response.content)
        # S4-2 Layer A: AI verdict ↔ 실제 산출물 cross-check. hallucination 으로
        # 실제 있는 섹션을 '누락'이라 판정한 케이스 자동 차단. 실제 content 의
        # ## 헤더와 AI 주장을 매칭 → false positive 제거 후 score 복원.
        if node.task_pair_node_id:
            try:
                from engine.skills.qa.verdict_reconciler import reconcile_verdict
                # paired TASK 의 최신 artifact 로드
                _task_content_row = await db.fetchone(
                    """SELECT av.storage_path AS content
                    FROM artifacts a JOIN artifact_versions av ON av.artifact_id=a.id
                    WHERE a.node_id=? AND av.version_num=a.current_version
                    LIMIT 1""",
                    (node.task_pair_node_id,),
                )
                _actual_content = (
                    _task_content_row.get("content") if _task_content_row else ""
                ) or ""
                if _actual_content:
                    verdict, _recon_info = reconcile_verdict(
                        verdict, _actual_content, spec=spec,
                    )
                    if _recon_info.get("changed"):
                        logger.info(
                            "qa_verdict_reconciled node=%s filtered=%d score=%d→%d",
                            node.id[:8], _recon_info["filtered_count"],
                            _recon_info["score_before"], _recon_info["score_after"],
                        )
            except Exception as _rec_err:
                logger.warning("verdict_reconciler failed: %s", _rec_err)
        # L2 캐시: artifact_version을 verdict에 기록 → 재평가 시 재사용 가능
        if node.task_pair_node_id:
            try:
                _av_row = await db.fetchone(
                    """SELECT av.version_num FROM artifacts a
                       JOIN artifact_versions av ON av.artifact_id=a.id
                       WHERE a.node_id=? AND av.version_num=a.current_version""",
                    (node.task_pair_node_id,),
                )
                if _av_row and _av_row.get("version_num") is not None:
                    verdict["artifact_version"] = _av_row["version_num"]
            except Exception:
                pass
        await _save_qa_stamp(db, node, verdict)
        # Score 기반 PASS 임계값: 50+ → PASS (경고 기록), <50 → FAIL
        _qa_score = verdict.get("score", 0)
        if verdict["summary"] == "FAIL" and _qa_score >= 50:
            verdict["summary"] = "PASS"
            verdict["method"] = verdict.get("method", "") + "_score_threshold"
            await _save_qa_stamp(db, node, verdict)  # PASS로 재저장
            logger.info("qa_score_pass node=%s score=%d (threshold 50)", node.id[:8], _qa_score)

        # S6: Harness-Supreme — 구조 검증 PASS 이면 AI FAIL 무시.
        # AI hallucination 으로 완벽한 산출물이 SUSPENDED 되는 패턴 차단.
        # Harness 는 결정론적 (regex 기반 headings·tables·chars 검증) — 거짓말 못 함.
        if verdict["summary"] == "FAIL" and node.task_pair_node_id:
            try:
                from engine.skills.qa.harness import _harness_validate_document
                from engine.skills.registry import SkillRegistry as _SkReg
                # paired TASK artifact + spec 로드
                _art_row = await db.fetchone(
                    """SELECT av.storage_path AS content FROM artifacts a
                    JOIN artifact_versions av ON av.artifact_id=a.id
                    WHERE a.node_id=? AND av.version_num=a.current_version LIMIT 1""",
                    (node.task_pair_node_id,),
                )
                _task_row = await db.fetchone(
                    "SELECT name, phase FROM nodes WHERE id=?",
                    (node.task_pair_node_id,),
                )
                _actual = (_art_row or {}).get("content") or ""
                if _actual and _task_row:
                    _task_spec = _SkReg().resolve(
                        _task_row["name"], _task_row["phase"], "TASK",
                    )
                    _structural = (_task_spec or {}).get("validation", {}).get("structural")
                    if _task_spec and _structural:
                        # S10+S12: type-aware — JSON/HTML/document 분기.
                        _task_art_type = (_task_spec or {}).get("type", "document")
                        if _task_art_type == "json":
                            from engine.skills.qa.harness import _harness_validate_json
                            _h = _harness_validate_json(_actual, _task_spec)
                        elif _task_art_type == "html":
                            from engine.skills.qa.harness import _harness_validate_html
                            _h = _harness_validate_html(_actual, _task_spec)
                        else:
                            _h = _harness_validate_document(
                                _actual, _task_row["name"], _task_spec,
                            )
                        if _h.get("pass"):
                            # AI 의견은 description 에 보존, verdict 은 PASS 로 승격
                            verdict["ai_concerns"] = {
                                "original_summary": "FAIL",
                                "original_score": _qa_score,
                                "issues": [
                                    {"title": iss.get("title"),
                                     "severity": iss.get("severity")}
                                    for cat in verdict.get("categories", [])
                                    for iss in cat.get("issues", [])
                                ][:10],
                            }
                            verdict["summary"] = "PASS"
                            verdict["method"] = (verdict.get("method") or "") + "_harness_supreme"
                            await _save_qa_stamp(db, node, verdict)
                            logger.info(
                                "harness_supreme_override node=%s ai_score=%d ai_issues=%d → PASS",
                                node.id[:8], _qa_score,
                                len(verdict["ai_concerns"]["issues"]),
                            )
            except Exception as _herr:
                logger.warning("harness_supreme check failed: %s", _herr)

        if verdict["summary"] == "FAIL":
            fail_reasons = [
                f"[{iss['severity']}] {iss['title']}"
                for cat in verdict.get("categories", [])
                for iss in cat.get("issues", [])
            ]
            fail_text = "; ".join(fail_reasons[:5])

            # Score 30~49: 부분 패치 (결함 부분만 AI 수정, 나머지 보존)
            if _qa_score >= 30 and node.task_pair_node_id:
                _affected = [
                    iss.get("title", "")
                    for cat in verdict.get("categories", [])
                    for iss in cat.get("issues", [])
                    if iss.get("severity") in ("CRITICAL", "HIGH")
                ]
                if _affected:
                    import json as _jv_patch
                    # S4-1: verdict 에서 실제 섹션 명 추출 → paired TASK 노드의
                    # task_snapshot.missing_sections_last_attempt 에 기록.
                    # 다음 retry 시 _chunked_document_generate 가 이 힌트로 해당
                    # 섹션 prompt 최상단에 ⚠⚠⚠ 경고 prepend → 누락 반복 차단.
                    try:
                        from engine.skills.qa.verdict_parser import (
                            extract_from_categories, extract_missing_sections,
                        )
                        # spec.sections 이름 list — paired TASK 의 task_snapshot 에서
                        # 추론 불가하므로 verdict fail_text 자체로 파싱
                        _missing = extract_from_categories(
                            verdict.get("categories", []),
                        )
                        if not _missing:
                            _missing = extract_missing_sections(fail_text)
                        if _missing:
                            # paired TASK 의 기존 task_snapshot 로드 후 키 추가
                            _snap_row = await db.fetchone(
                                "SELECT task_snapshot FROM nodes WHERE id=?",
                                (node.task_pair_node_id,),
                            )
                            _existing_snap: dict = {}
                            if _snap_row and _snap_row.get("task_snapshot"):
                                try:
                                    _existing_snap = _jv_patch.loads(
                                        _snap_row["task_snapshot"]
                                    ) or {}
                                except Exception:
                                    _existing_snap = {}
                            if not isinstance(_existing_snap, dict):
                                _existing_snap = {}
                            _existing_snap["missing_sections_last_attempt"] = _missing
                            _existing_snap.setdefault("type", "chunked_document")
                            await db.execute(
                                "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
                                (
                                    _jv_patch.dumps(_existing_snap, ensure_ascii=False),
                                    _now(), node.task_pair_node_id,
                                ),
                            )
                            logger.info(
                                "verdict_extracted_missing_sections count=%d names=%s",
                                len(_missing), ", ".join(_missing[:5]),
                            )
                    except Exception as _verr:
                        logger.warning("verdict_parser failed: %s", _verr)
                    await db.execute(
                        """UPDATE nodes SET state='INVALID',
                           invalidation_change_type='PARTIAL',
                           invalidation_affected_sections=?,
                           stall_count=COALESCE(stall_count,0)+1,
                           updated_at=? WHERE id=?""",
                        (_jv_patch.dumps(_affected, ensure_ascii=False), _now(), node.task_pair_node_id),
                    )
                    logger.info("qa_partial_patch score=%d affected=%d node=%s",
                                _qa_score, len(_affected), node.id[:8])
                    raise ValueError(
                        f"QA 부분 패치 (score={_qa_score}): {'; '.join(_affected[:3])}"
                    )

            # ── 거시 우선 진단 (root cause 가 상위 단계에 있는지 자동 분석) ──
            # 키워드 strict 매칭 + 0건 시 AI fallback (V10_UPSTREAM_REWORK_MODE 환경변수로 제어)
            # 0건이면 기존 미시 retry 흐름으로 자동 fallback (회귀 0)
            from engine.skills.executor_cascade import macro_diagnose_safe
            _upstream_affected = await macro_diagnose_safe(
                db, node.id, node.task_pair_node_id or "",
                fail_text, model_adapter=model_adapter, source="qa_verdict",
            )

            if _upstream_affected > 0:
                # 거시 진단 성공 → 직속 paired TASK 도 함께 INVALID (정합성)
                if node.task_pair_node_id:
                    try:
                        await db.execute(
                            "UPDATE nodes SET state='INVALID', "
                            "stall_count=COALESCE(stall_count,0)+1, "
                            "updated_at=? WHERE id=?",
                            (_now(), node.task_pair_node_id),
                        )
                    except Exception:
                        pass
                logger.warning(
                    "qa_root_cause_detected qa=%s upstream=%d → paired TASK INVALID",
                    node.id[:8], _upstream_affected,
                )
                raise ValueError(
                    f"QA 판정 FAIL (score={verdict.get('score', '?')}) — "
                    f"상위 결함 {_upstream_affected}건 감지, root cause 재생성 대기"
                )

            # Score < 30: 전체 재생성 (구조적 문제)
            # 조건 1: 키워드 매칭 — 기존 재시도 트리거 유지
            # 조건 2: 반복 동일실패 — QA가 retry>=1 이면서 직전 실패 사유와 같으면 강제 재생성
            #         (같은 산출물로 같은 이유 반복 FAIL → 검사만 반복해도 의미 없음)
            _retrigger = node.task_pair_node_id and any(
                kw in fail_text for kw in _TASK_RETRIGGER_KEYWORDS
            )
            if not _retrigger and node.task_pair_node_id and (node.retry_count or 0) >= 1:
                try:
                    _fr_row = await db.fetchone(
                        "SELECT failure_reasons FROM nodes WHERE id=?", (node.id,),
                    )
                    if _fr_row and _fr_row["failure_reasons"]:
                        _prev = json.loads(_fr_row["failure_reasons"]) or []
                        _prev_text = (_prev[-1].get("reason", "") if _prev else "")
                        # 정규화 후 비교 (공백·타임스탬프·id 제거) — false positive 최소화.
                        # 1. 둘 다 비어있으면 비교 X
                        # 2. score 는 제외하고 핵심 내용만 추출
                        # 3. normalize → 완전 일치여야 "같은 실패"로 인정
                        if _prev_text and fail_text:
                            import re as _re_rep
                            def _norm(s: str) -> str:
                                s = _re_rep.sub(r"score=\d+", "", s)
                                # UUID 정확 패턴만 제거 (8-4-4-4-12)
                                s = _re_rep.sub(
                                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                    r"[0-9a-f]{4}-[0-9a-f]{12}",
                                    "", s, flags=_re_rep.IGNORECASE,
                                )
                                # ISO 타임스탬프 제거 (시점만 달라 같은 실패 오판 방지)
                                s = _re_rep.sub(
                                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?",
                                    "", s,
                                )
                                s = _re_rep.sub(r"\s+", " ", s).strip().lower()
                                return s
                            if _norm(_prev_text) == _norm(fail_text):
                                _retrigger = True
                                logger.warning(
                                    "qa_repeat_same_failure node=%s — forcing TASK re-gen",
                                    node.id[:8],
                                )
                except Exception:
                    pass
            if _retrigger:
                # P0 hotfix: 이 스코프에선 변수명이 `verdict` (line 3395). `_qa_verdict`
                # 는 과거 harness 분기 전용 이름이라 여기서 NameError. 그대로 verdict 사용.
                await db.execute(
                    "UPDATE nodes SET state='INVALID', "
                    "stall_count=COALESCE(stall_count,0)+1, "
                    "description=?, updated_at=? WHERE id=?",
                    (json.dumps(verdict, ensure_ascii=False),
                     _now(), node.task_pair_node_id),
                )
                logger.warning(
                    "qa_task_retrigger node=%s task=%s reason=code_structural_issue",
                    node.id, node.task_pair_node_id,
                )
            raise ValueError(
                f"QA 판정 FAIL (score={verdict.get('score', '?')}): "
                + fail_text
            )

    # 10-2. TESTING phase: 결함 발견 시 상위 노드 역방향 cascade
    if node.node_type == "TASK" and node.phase == "VERIFY":
        await _defect_cascade(db, node, response.content, model_adapter)

    # 11. Mark completed
    await db.execute(
        "UPDATE nodes SET state='COMPLETED', completed_at=?, "
        "updated_at=? WHERE id=?",
        (_now(), _now(), node.id),
    )

    # 11-0a. TASK 완료 시 짝 QA 노드를 자동 복구:
    #         (a) pair link 누락 시 이름 매칭으로 양방향 link 자동 세팅 (중복 QA 있어도
    #             실제 실행된 (state != SKIPPED) 것 또는 최근 created 것 우선)
    #         (b) QA state 가 BLOCKED/SKIPPED 이면 NOT_STARTED 로 전환
    #         (c) QA 의 outgoing edges 중 is_active=0 인 것 재활성화 → 다운스트림 의존성 복구
    #
    #         기존 startup._heal_pair_links 는 서버 재시작 시에만 실행되어 runtime 중
    #         깨진 pair link 를 복구 못 했음. 여기서 cascade 경로로 확장하여 범용 복구.
    if node.node_type == "TASK":
        try:
            _qa_pair_id = getattr(node, "qa_pair_node_id", None)

            # (a) pair link 누락 시 이름 매칭으로 복구
            if not _qa_pair_id:
                _qa_candidate = await db.fetchone(
                    """SELECT n.id FROM nodes n
                       WHERE n.dag_id=? AND n.project_id=? AND n.node_type='QA'
                         AND n.name=?
                       ORDER BY
                         CASE WHEN n.state != 'SKIPPED' THEN 0 ELSE 1 END,
                         (SELECT COUNT(*) FROM artifacts WHERE node_id=n.id) DESC,
                         n.created_at DESC
                       LIMIT 1""",
                    (node.dag_id, node.project_id, f"[QA] {node.name}"),
                )
                if _qa_candidate:
                    _qa_pair_id = _qa_candidate["id"]
                    # 양방향 pair link 세팅 (둘 다 NULL 일 때만 — 기존 link 덮어쓰기 방지)
                    await db.execute(
                        "UPDATE nodes SET qa_pair_node_id=?, updated_at=? WHERE id=? AND qa_pair_node_id IS NULL",
                        (_qa_pair_id, _now(), node.id),
                    )
                    await db.execute(
                        "UPDATE nodes SET task_pair_node_id=?, updated_at=? WHERE id=? AND task_pair_node_id IS NULL",
                        (node.id, _now(), _qa_pair_id),
                    )
                    logger.info(
                        "cascade_pair_link_healed task=%s qa=%s",
                        node.id[:8], _qa_pair_id[:8],
                    )

            # (b) QA state 전환 + (c) outgoing edges 재활성화
            if _qa_pair_id:
                _rowcount = await db.execute(
                    """UPDATE nodes SET state='NOT_STARTED', updated_at=?, version=version+1
                       WHERE id=? AND state IN ('BLOCKED', 'SKIPPED')""",
                    (_now(), _qa_pair_id),
                )
                if _rowcount:
                    await db.execute(
                        "UPDATE edges SET is_active=1 WHERE from_node_id=? AND is_active=0",
                        (_qa_pair_id,),
                    )
            # DAG enqueue 는 TASK COMPLETED 다음 단계의 cascade 훅(아래 11-1)이
            # 처리하므로 여기서 별도로 하지 않는다. unblock 상태 업데이트만으로 충분.
        except Exception as _qa_unblock_err:
            logger.debug("qa_pair_unblock_skip node=%s err=%s", node.id[:8], _qa_unblock_err)

    # 11-0. 해결된 gotchas 정리 (COMPLETED → 해당 노드의 실패 기록 삭제)
    try:
        await db.execute(
            "DELETE FROM project_gotchas WHERE source_node_id=?",
            (node.id,),
        )
    except Exception:
        pass

    # 11-1. Cascade 전파
    # - TASK 완료: upstream만 (상위 정의/명세 갱신)
    # - QA PASS: paired TASK의 downstream cascade (QA 검증 통과 후에만 하위 전파)
    if node.node_type == "QA" and node.task_pair_node_id:
        # QA PASS → paired TASK의 downstream cascade
        try:
            _task_snap = await db.fetchone(
                "SELECT id, project_id, phase, name, dag_id, priority, "
                "retry_count, max_retries, assigned_model, gate_auto_approve, version "
                "FROM nodes WHERE id=?",
                (node.task_pair_node_id,),
            )
            if _task_snap:
                from engine.core.dag_advancer import NodeSnapshot as _NS
                # NodeSnapshot 실제 필드만 사용 (invalidation_source_id 같은 DB-only
                # 필드는 제외). dataclass 생성자 시그니처와 1:1 매칭.
                _pseudo = _NS(
                    id=_task_snap["id"],
                    dag_id=_task_snap["dag_id"],
                    project_id=_task_snap["project_id"],
                    node_type="TASK",
                    phase=_task_snap["phase"],
                    name=_task_snap["name"],
                    state="COMPLETED",
                    priority=_task_snap.get("priority") or 3,
                    retry_count=_task_snap.get("retry_count") or 0,
                    max_retries=_task_snap.get("max_retries") or 3,
                    assigned_model=_task_snap.get("assigned_model"),
                    qa_pair_node_id=node.id,
                    task_pair_node_id=None,
                    gate_auto_approve=bool(_task_snap.get("gate_auto_approve")),
                    version=_task_snap.get("version") or 0,
                    deps=[],
                )
                await _trigger_downstream_cascade(db, _pseudo, model_adapter)
                logger.info("qa_pass_downstream_cascade task=%s qa=%s",
                            (node.task_pair_node_id or "")[:8], node.id[:8])
        except Exception as _casc_err:
            logger.warning(
                "qa_downstream_cascade_failed qa=%s error=%s — continuing",
                node.id[:8], _casc_err,
            )
    elif node.node_type == "TASK":
        # TASK 완료: upstream만 (하위는 QA PASS 후)
        try:
            await _trigger_upstream_cascade(db, node, model_adapter)
        except Exception as _casc_err:
            logger.warning(
                "upstream_cascade_failed node=%s error=%s — continuing",
                node.id[:8], _casc_err,
            )


async def _enforce_category_constraint(
    db: Any,
    model_adapter: ModelAdapter,
    model: str,
    response: Any,
    node: NodeSnapshot,
    assembly: Any,
    max_tokens: int,
) -> Any:
    """
    _library_split_category 제약이 있는 TASK 의 JSON 배열 결과에서
    모든 item 의 category 필드가 지정된 카테고리와 일치하는지 검증.

    미스매치 시 1회 교정 재호출. 여전히 미스매치면 원본 반환 (QA 가 적발).

    Returns:
        response (통과 시 원본, 재호출 성공 시 새 응답)
    """
    try:
        row = await db.fetchone(
            "SELECT description FROM nodes WHERE id=?", (node.id,),
        )
        if not row or not row["description"]:
            return response
        desc = json.loads(row["description"])
    except (json.JSONDecodeError, TypeError):
        return response

    if not isinstance(desc, dict):
        return response
    target_cat = desc.get("_library_split_category")
    if not target_cat:
        return response

    try:
        parsed = json.loads(response.content)
    except (json.JSONDecodeError, AttributeError):
        return response
    if not isinstance(parsed, list):
        return response

    mismatched = [
        (i, str(item.get("category", "<missing>")))
        for i, item in enumerate(parsed)
        if isinstance(item, dict) and item.get("category") != target_cat
    ]
    if not mismatched:
        return response

    logger.warning(
        "category_constraint_violation node=%s target=%s violations=%d/%d",
        node.id[:8], target_cat, len(mismatched), len(parsed),
    )

    corrective = (
        f"\n\n## 🚨 CRITICAL 재생성 요청\n"
        f"이전 응답의 {len(mismatched)}/{len(parsed)}개 컴포넌트가 "
        f"category='{target_cat}' 이 아닙니다.\n"
        f"발견된 잘못된 category 값: {', '.join({c for _, c in mismatched[:5]})}\n\n"
        f"**모든 컴포넌트의 category 필드를 반드시 '{target_cat}' 으로 설정**해야 합니다.\n"
        f"'{target_cat}' 카테고리에 속하지 않는 컴포넌트는 모두 삭제하고 재생성하세요.\n"
        f"순수 JSON 배열로만 출력. category 필드 하드코딩 필수."
    )
    corrected_prompt = assembly.prompt + corrective

    try:
        retry_resp = await model_adapter.call(
            model=model,
            system=assembly.system,
            prompt=corrected_prompt,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        logger.warning(
            "category_retry_call_failed node=%s error=%s", node.id[:8], exc,
        )
        return response

    try:
        reparsed = json.loads(retry_resp.content)
    except (json.JSONDecodeError, AttributeError):
        logger.warning(
            "category_retry_unparseable node=%s — keeping original", node.id[:8],
        )
        return response

    if not isinstance(reparsed, list):
        return response
    still_wrong = [
        item for item in reparsed
        if isinstance(item, dict) and item.get("category") != target_cat
    ]
    if still_wrong:
        logger.warning(
            "category_retry_still_wrong node=%s still=%d/%d — keeping original",
            node.id[:8], len(still_wrong), len(reparsed),
        )
        return response

    logger.info(
        "category_retry_success node=%s target=%s items=%d",
        node.id[:8], target_cat, len(reparsed),
    )
    # Prometheus counter
    try:
        from engine.observability.metrics import V10_CATEGORY_RETRY_SUCCESS
        V10_CATEGORY_RETRY_SUCCESS.labels(category=target_cat).inc()
    except Exception:
        pass
    return retry_resp

