"""
Executor Partial-patch helpers — check/clear/execute PARTIAL invalidation mode.

Extracted from executor.py for maintainability.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from engine.core.dag_advancer import NodeSnapshot
from engine.skills.utils import _now

logger = logging.getLogger(__name__)


async def _check_partial_mode(db: Any, node: "NodeSnapshot") -> dict | None:
    """
    노드가 PARTIAL 패치 모드 대상인지 확인.
    INVALID 상태 + invalidation_change_type='PARTIAL' 이면 정보 반환.
    그 외엔 None.
    """
    if node.state != "INVALID":
        return None
    row = await db.fetchone(
        """SELECT invalidation_change_type, invalidation_affected_sections,
                  invalidation_source_id
           FROM nodes WHERE id=?""",
        (node.id,),
    )
    if not row or row["invalidation_change_type"] != "PARTIAL":
        return None

    import json as _json
    sections: list[str] = []
    if row["invalidation_affected_sections"]:
        try:
            sections = _json.loads(row["invalidation_affected_sections"])
        except Exception as exc:
            logger.debug("invalidation_sections_parse_failed error=%s", exc)
            sections = []

    if not sections:
        # 섹션 없으면 PARTIAL 패치 불가 → None 반환해서 일반 재실행
        return None

    return {
        "source_node_id": row["invalidation_source_id"],
        "affected_sections": sections,
    }


async def _clear_invalidation_meta(db: Any, node_id: str, now: str) -> None:
    """노드의 invalidation 메타데이터 클리어."""
    await db.execute(
        "UPDATE nodes SET invalidation_change_type=NULL, "
        "invalidation_affected_sections=NULL, updated_at=? WHERE id=?",
        (now, node_id),
    )


async def _execute_partial_patch(
    db: Any,
    node: "NodeSnapshot",
    assembler: "ContextAssembler",
    model_adapter: "ModelAdapter",
    budget_enforcer: "BudgetEnforcer",
    partial_info: dict,
) -> None:
    """
    PARTIAL 패치 모드 실행.
    현재 artifact(DB 직접 로드) → 지정 섹션만 수정 → 저장 → QA 일관성 검증 강제 재실행.

    수정 이유:
      - storage_path는 파일 경로가 아니라 artifact 콘텐츠 자체 (DB 직접 사용)
      - art_type을 spec에서 조회 (하드코딩 제거)
      - QA 재실행 시 일관성 검증 지시 추가 (AI 오판 보호망)
    """
    from engine.ai.context_assembler import NodeContext
    from engine.skills.artifact.loader import _load_project_context, _load_deltas
    from engine.skills.artifact.saver import _save_artifact
    from engine.skills.registry import SkillRegistry
    from engine.skills.executor_cascade import _trigger_downstream_cascade

    now = _now()
    affected_sections = partial_info["affected_sections"]

    # S4-1: affected_sections 가 검증 항목명("required_sections" 등)뿐이면
    # verdict_parser 로 실제 섹션 명 대체 시도. consistency_note 가 의미있게 됨.
    try:
        from engine.skills.qa.verdict_parser import extract_missing_sections
        # verdict text 합성 — issues title 들을 자유 텍스트로
        _verdict_text = "; ".join(
            str(s) for s in affected_sections if isinstance(s, str)
        )
        _real_sections = extract_missing_sections(_verdict_text)
        if _real_sections:
            affected_sections = _real_sections
            logger.info(
                "partial_affected_sections_refined count=%d names=%s",
                len(_real_sections), ", ".join(_real_sections[:5]),
            )
    except Exception:
        pass

    # 1. 현재 artifact 콘텐츠 로드 (storage_path = 콘텐츠 자체, 파일 경로 아님)
    current_art_row = await db.fetchone(
        """SELECT av.storage_path AS content, av.version_num, a.artifact_type
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
                AND av.version_num = a.current_version
           WHERE a.node_id=?
           ORDER BY a.created_at DESC LIMIT 1""",
        (node.id,),
    )
    if not current_art_row:
        # artifact 없음 → 메타 클리어 후 일반 재실행으로 폴백
        logger.warning("partial_patch_no_artifact node=%s → fallback full run", node.id[:8])
        await _clear_invalidation_meta(db, node.id, now)
        raise ValueError("PARTIAL 패치 전환: artifact 없음 → 전체 재실행")

    # storage_path 컬럼에 콘텐츠가 직접 저장됨 (DB 컬럼명이 오해 소지 있음)
    current_artifact_content: str = current_art_row["content"] or ""

    # 2. art_type: spec 조회 (한계 3 수정 — 하드코딩 제거)
    _registry = SkillRegistry()
    _spec = _registry.resolve(node.name, node.phase, node.node_type)
    art_type = (_spec.get("type", "document") if _spec else None) or current_art_row["artifact_type"] or "document"

    # 3. 프로젝트 컨텍스트 + deltas 로드
    project = await _load_project_context(db, node.project_id)
    deltas = await _load_deltas(db, node)

    # 4. NodeContext 조립
    node_ctx = NodeContext(
        node_id=node.id,
        node_type=node.node_type,
        name=node.name,
        description=f"[PARTIAL 패치] 다음 섹션을 upstream 변경에 맞게 업데이트하라: "
                    + ", ".join(affected_sections),
        phase=node.phase,
        project_id=node.project_id,
        engagement_id=project.engagement_id,
        retry_count=node.retry_count,
        failure_reasons=[],
        assigned_model=node.assigned_model,
        constitution_version_id=None,
    )

    # 5. 패치 모드 context 조립
    # chunked_html* 은 task_snapshot.completed_items 캐시가 별도로 존재하므로
    # 이전 artifact 전체 HTML 주입 금지 (DOCTYPE 중복/section id 오염 누적 차단).
    _is_chunked_html = isinstance(art_type, str) and art_type.startswith("chunked_html")
    _ctx_artifact = None if _is_chunked_html else (current_artifact_content or None)
    if _is_chunked_html:
        logger.info(
            "partial_patch_skip_artifact_injection node=%s art_type=%s reason=chunked_html_cache_used",
            node.id[:8], art_type,
        )
    assembly = assembler.assemble(
        node_ctx, project, deltas,
        patch_mode=True,
        affected_sections=affected_sections,
        current_artifact=_ctx_artifact,
    )

    # 6. 예산 체크 + AI 호출
    engagement_id = project.engagement_id
    try:
        if engagement_id:
            max_tokens = await budget_enforcer.pre_call_check(
                node_id=node.id,
                engagement_id=engagement_id,
                phase=node.phase,
                prompt=assembly.system + assembly.prompt,
            )
        else:
            from engine.core.budget_enforcer import TOKEN_BUDGET
            max_tokens = TOKEN_BUDGET["max_output"]
    except Exception as exc:
        logger.debug("budget_pre_call_check_failed error=%s", exc)
        from engine.core.budget_enforcer import TOKEN_BUDGET
        max_tokens = TOKEN_BUDGET["max_output"]

    model = node.assigned_model or "claude-sonnet-4-6"
    response = await model_adapter.call(
        model=model,
        system=assembly.system,
        prompt=assembly.prompt,
        max_tokens=max_tokens,
    )

    # 7. 패치 결과 저장
    await _save_artifact(db, node, response.content, art_type)

    # 7-1. 패치 무결성 하네스 검증 (3-C) — AI QA 전에 코드로 drift 조기 감지
    from engine.skills.qa.harness import _harness_validate_partial_patch
    integrity = _harness_validate_partial_patch(
        old_artifact=current_artifact_content,
        new_artifact=response.content,
        affected_sections=affected_sections,
    )
    if not integrity["pass"]:
        # 무결성 실패 → PARTIAL 메타 클리어 + ValueError → executor가 전체 재실행으로 폴백
        logger.warning(
            "partial_patch_integrity_fail node=%s failures=%s → full rerun",
            node.id[:8], integrity["failures"],
        )
        await _clear_invalidation_meta(db, node.id, now)
        raise ValueError(
            f"PARTIAL 패치 무결성 실패 → 전체 재실행: {'; '.join(integrity['failures'])}"
        )

    # 8. QA pair 노드 강제 재실행 + 일관성 검증 지시 주입 (위험 1 보호망 강화)
    # PARTIAL 패치 후 QA는 수정 섹션 + 나머지 섹션 일관성을 추가로 검증해야 함
    if node.qa_pair_node_id:
        consistency_note = (
            "\n\n[PARTIAL_PATCH_QA 주의] 이 산출물은 부분 패치로 업데이트되었습니다. "
            f"수정 대상 섹션: {', '.join(affected_sections)}\n"
            "수정된 섹션과 나머지 섹션 간 일관성(용어, 구조, 논리)을 특별히 검증하세요. "
            "불일치 발견 시 반드시 FAIL 판정하세요."
        )
        # QA 노드 description에 일관성 검증 지시 추가
        qa_row = await db.fetchone("SELECT description FROM nodes WHERE id=?", (node.qa_pair_node_id,))
        existing_desc = (qa_row["description"] or "") if qa_row else ""
        # 이미 주입된 경우 중복 방지
        if "[PARTIAL_PATCH_QA 주의]" not in existing_desc:
            new_desc = existing_desc + consistency_note
            await db.execute(
                """UPDATE nodes
                   SET state='INVALID',
                       description=?,
                       invalidation_change_type=NULL,
                       invalidation_affected_sections=NULL,
                       updated_at=?
                   WHERE id=? AND state IN ('COMPLETED', 'SKIPPED', 'BLOCKED', 'NOT_STARTED', 'INVALID')""",
                (new_desc, now, node.qa_pair_node_id),
            )
        else:
            await db.execute(
                """UPDATE nodes
                   SET state='INVALID',
                       invalidation_change_type=NULL,
                       invalidation_affected_sections=NULL,
                       updated_at=?
                   WHERE id=? AND state IN ('COMPLETED', 'SKIPPED', 'BLOCKED', 'NOT_STARTED', 'INVALID')""",
                (now, node.qa_pair_node_id),
            )
        logger.info(
            "partial_patch_qa_retrigger node=%s qa=%s consistency_check=injected",
            node.id[:8], node.qa_pair_node_id[:8],
        )

    # 9. 현재 노드 완료 + 메타 클리어
    await db.execute(
        """UPDATE nodes
           SET state='COMPLETED',
               invalidation_change_type=NULL,
               invalidation_affected_sections=NULL,
               completed_at=?, updated_at=?
           WHERE id=?""",
        (now, now, node.id),
    )

    # 10. Downstream cascade: 패치 결과가 이전과 달라졌으면 직접 downstream에 전파
    try:
        await _trigger_downstream_cascade(db, node, model_adapter)
    except Exception as _casc_err:
        logger.warning(
            "partial_patch_cascade_failed node=%s error=%s — continuing",
            node.id[:8], _casc_err,
        )

    logger.info(
        "partial_patch_complete node=%s sections=%d art_type=%s",
        node.id[:8], len(affected_sections), art_type,
    )
