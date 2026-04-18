"""
Executor Cascade helpers — downstream cascade classification and triggering.

Extracted from executor.py for maintainability.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


async def _cascade_for_node(
    db: Any,
    source_node_id: str,
    ds_node: dict,
    diff: str,
    old_content: str,
    model_adapter: Any,
) -> None:
    """
    단일 직접 downstream 노드에 대한 cascade 분류 + INVALID 전이.

    직접 downstream만 처리 — deep BFS 없음.
    각 노드가 재실행 완료 시 _trigger_downstream_cascade가 다시 호출되어
    결과가 달라진 경우에만 다음 단계로 자연 전파.
    결과가 동일하면 generate_diff → 빈 diff → 전파 중단.
    """
    from engine.core.cascade import CascadeInvalidator
    from engine.ai.change_classifier import classify_change

    classification = await classify_change(
        diff=diff,
        downstream_node_name=ds_node["name"],
        downstream_artifact_type=ds_node["node_type"],
        model_adapter=model_adapter,
        old_content=old_content,
    )
    change_type = classification["type"]
    affected_sections = classification.get("affected_sections", [])

    # 직접 downstream 노드만 마킹 + INVALID 전이 (deep BFS 제거)
    now = _now()
    # CONTEXTUAL은 전체 재실행이므로 섹션 정보 불필요 → NULL로 저장
    sections_json = json.dumps(affected_sections, ensure_ascii=False) if change_type == "PARTIAL" else None
    await db.execute(
        """UPDATE nodes
           SET invalidation_pending=1, invalidation_source_id=?,
               invalidation_queued_at=?, invalidation_change_type=?,
               invalidation_affected_sections=?, updated_at=?
           WHERE id=? AND invalidation_pending=0""",
        (source_node_id, now, change_type, sections_json, now, ds_node["id"]),
    )

    cascade = CascadeInvalidator(db)
    await cascade.phase2_apply_invalid(ds_node["id"])

    # 버그 2: cascade INVALID 시 retry_count 리셋 (이전 실패 횟수가 재실행을 막지 않도록)
    await db.execute(
        "UPDATE nodes SET retry_count=0 WHERE id=? AND state='INVALID'",
        (ds_node["id"],),
    )

    # 버그 3: 같은 phase의 COMPLETED GATE 리셋 (GATE가 고아 COMPLETED로 남으면 후행 단계가 진행됨)
    await db.execute(
        """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, updated_at=?
           WHERE dag_id=(SELECT dag_id FROM nodes WHERE id=?)
           AND node_type='GATE' AND state='COMPLETED'
           AND phase=(SELECT phase FROM nodes WHERE id=?)""",
        (now, ds_node["id"], ds_node["id"]),
    )

    # QA pair 노드 리셋: TASK가 INVALID되면 이전 QA 결과도 무효
    await db.execute(
        """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, retry_count=0, updated_at=?
           WHERE (task_pair_node_id=? OR qa_pair_node_id=?)
           AND node_type='QA' AND state IN ('COMPLETED', 'BLOCKED')""",
        (now, ds_node["id"], ds_node["id"]),
    )

    logger.info(
        "downstream_cascade_node node=%s ds=%s type=%s",
        source_node_id[:8], ds_node["id"][:8], change_type,
    )

    # cascade 후 advancer에 DAG enqueue — INVALID 노드가 즉시 픽업되도록
    try:
        dag_row = await db.fetchone(
            "SELECT dag_id FROM nodes WHERE id=?", (ds_node["id"],),
        )
        if dag_row:
            from api.server import _dag_advancer
            if _dag_advancer:
                await _dag_advancer.enqueue(dag_row["dag_id"])
    except Exception:
        pass  # advancer 없어도 다음 주기에 자연 픽업됨


async def _trigger_downstream_cascade(
    db: Any,
    node: "NodeSnapshot",
    model_adapter: "ModelAdapter",
) -> None:
    """
    노드 완료 후 artifact 변경 여부 체크 → 변경 시 downstream cascade 트리거.
    이전 버전이 없으면 (첫 실행) cascade 없음.

    수정 이유:
      - storage_path는 파일 경로가 아니라 콘텐츠 자체 (DB 직접 사용)
      - downstream 노드별 개별 분류 (단일 대표 분류 → 오분류 제거)
      - asyncio.create_task로 백그라운드 실행 (동기 블로킹 제거)
    """
    from engine.ai.change_classifier import generate_diff

    # 이전 버전 artifact 존재 여부 확인 (version_num >= 2 이면 재실행)
    # storage_path 컬럼 = 콘텐츠 자체 (파일 경로 아님)
    versions = await db.fetchall(
        """SELECT av.version_num, av.storage_path AS content
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE a.node_id=?
           ORDER BY av.version_num DESC
           LIMIT 2""",
        (node.id,),
    )
    if len(versions) < 2:
        # 첫 실행이지만, downstream 중 이미 COMPLETED인 노드가
        # 이 노드보다 먼저 완료됐다면 INVALID 처리 (선행 없이 완료된 케이스)
        await _invalidate_early_completed_downstreams(db, node)
        return

    old_content: str = versions[1]["content"] or ""
    new_content: str = versions[0]["content"] or ""

    if not old_content or not new_content:
        return

    diff = generate_diff(old_content, new_content)
    if not diff:
        return  # 내용 동일 — cascade 불필요

    # ── diff GATE (3-A): 공백/포매팅 전용 변경 필터 ──
    # 실질 내용이 없는 변경(공백·빈 줄만)은 cascade 불필요
    _meaningful_changed = [
        ln for ln in diff.split("\n")
        if ln and ln[0] in ("+", "-")
        and not ln.startswith("+++") and not ln.startswith("---")
        and ln[1:].strip()   # 공백·탭만 있는 줄 제외
    ]
    if not _meaningful_changed:
        logger.debug("cascade_gate_skip node=%s reason=whitespace_only", node.id[:8])
        return

    # 직접 연결된 downstream 노드 전체 조회 (한계 1 수정 — 대표 1개 → 전체 개별 분류)
    downstream_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.node_type
           FROM edges e
           JOIN nodes n ON n.id = e.to_node_id
           WHERE e.from_node_id=? AND e.is_active=1""",
        (node.id,),
    )
    if not downstream_nodes:
        return

    async def _run_all():
        for ds in downstream_nodes:
            try:
                await _cascade_for_node(db, node.id, ds, diff, old_content, model_adapter)
            except Exception as _e:
                logger.warning(
                    "cascade_per_node_failed node=%s ds=%s error=%s",
                    node.id[:8], ds["id"][:8], _e,
                )

    task = asyncio.create_task(_run_all(), name=f"cascade-{node.id[:8]}")
    task.add_done_callback(_log_task_exception)


def _log_task_exception(task: asyncio.Task) -> None:
    """Background task의 미처리 예외를 로깅."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background_task_failed name=%s error=%s", task.get_name(), exc)


async def _trigger_upstream_cascade(
    db: Any,
    node: "NodeSnapshot",
    model_adapter: "ModelAdapter",
) -> None:
    """노드 완료 후 상위(upstream) COMPLETED 노드에 cascade 전파.

    하위가 변경되면 상위 정의/명세도 갱신 필요할 수 있음.
    예: 컴포넌트 라이브러리에 modal 추가 → 컴포넌트 정의서에도 반영.

    무한 루프 방지:
    - invalidation_source_id 체인 추적 — 원인 노드는 스킵
    - INVALID 상태 노드 스킵 (이미 cascade 처리 중)
    """
    from engine.ai.change_classifier import generate_diff

    # 이전 버전 존재해야 diff 가능
    versions = await db.fetchall(
        """SELECT av.version_num, av.storage_path AS content
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE a.node_id=?
           ORDER BY av.version_num DESC
           LIMIT 2""",
        (node.id,),
    )
    if len(versions) < 2:
        return

    old_content: str = versions[1]["content"] or ""
    new_content: str = versions[0]["content"] or ""
    if not old_content or not new_content:
        return

    diff = generate_diff(old_content, new_content)
    if not diff:
        return

    # 의미 있는 변경인지 체크
    _meaningful = [
        ln for ln in diff.split("\n")
        if ln and ln[0] in ("+", "-")
        and not ln.startswith("+++") and not ln.startswith("---")
        and ln[1:].strip()
    ]
    if not _meaningful:
        return

    # 직접 상위(upstream) 노드 조회: edges에서 이 노드가 to_node_id인 것
    upstream_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.node_type, n.state, n.invalidation_source_id
           FROM edges e
           JOIN nodes n ON n.id = e.from_node_id
           WHERE e.to_node_id=? AND e.is_active=1
             AND n.node_type='TASK'""",
        (node.id,),
    )
    if not upstream_nodes:
        return

    # 이 노드의 원인 노드 (루프 방지용)
    my_row = await db.fetchone(
        "SELECT invalidation_source_id FROM nodes WHERE id=?", (node.id,),
    )
    my_source = my_row["invalidation_source_id"] if my_row else None

    for us in upstream_nodes:
        # COMPLETED만 대상
        if us["state"] != "COMPLETED":
            continue
        # 원인 노드면 스킵 (루프 방지)
        if us["id"] == my_source:
            logger.debug("upstream_cascade_skip_source node=%s us=%s", node.id[:8], us["id"][:8])
            continue
        # GATE/QA 노드 제외
        if us["node_type"] != "TASK":
            continue

        try:
            await _cascade_for_node(db, node.id, us, diff, old_content, model_adapter)
            logger.info(
                "upstream_cascade_node node=%s upstream=%s name=%s",
                node.id[:8], us["id"][:8], us["name"],
            )
        except Exception as _e:
            logger.warning(
                "upstream_cascade_failed node=%s us=%s error=%s",
                node.id[:8], us["id"][:8], _e,
            )


async def _invalidate_early_completed_downstreams(
    db: Any, node: "NodeSnapshot",
) -> None:
    """첫 실행 완료된 노드의 downstream 중 먼저 COMPLETED된 노드를 INVALID 처리.

    선행 노드 없이 실행 완료된 하위 노드를 감지하여 재실행 트리거.
    - downstream.state == 'COMPLETED' AND completed_at < now
    - is_active=1 edge로 직접 연결된 노드만 대상
    - QA 쌍 리셋 + 같은 phase GATE 리셋 포함
    """
    now = _now()

    early_completed = await db.fetchall(
        """SELECT n.id, n.name, n.node_type, n.phase, n.dag_id
           FROM edges e
           JOIN nodes n ON n.id = e.to_node_id
           WHERE e.from_node_id = ? AND e.is_active = 1
             AND n.state = 'COMPLETED'
             AND n.completed_at < ?""",
        (node.id, now),
    )
    if not early_completed:
        return

    for ds in early_completed:
        # INVALID 처리
        await db.execute(
            """UPDATE nodes SET state='INVALID',
               invalidation_source_id=?, invalidation_change_type='CONTEXTUAL',
               retry_count=0, updated_at=?
               WHERE id=? AND state='COMPLETED'""",
            (node.id, now, ds["id"]),
        )

        # QA 쌍 리셋
        await db.execute(
            """UPDATE nodes SET state='NOT_STARTED', updated_at=?
               WHERE (task_pair_node_id=? OR qa_pair_node_id=?)
                 AND state NOT IN ('SKIPPED')""",
            (now, ds["id"], ds["id"]),
        )

        # 같은 phase GATE 리셋
        await db.execute(
            """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, updated_at=?
               WHERE dag_id=? AND node_type='GATE' AND state='COMPLETED'
                 AND phase=?""",
            (now, ds["dag_id"], ds["phase"]),
        )

        logger.info(
            "early_completed_invalidated upstream=%s downstream=%s name=%s",
            node.id[:8], ds["id"][:8], ds["name"],
        )


# ────────────────────────────────────────────────────────────────────────
# S2-5: 역방향 Rework — downstream QA FAIL 사유에 upstream artifact 참조가
# 명시되면 upstream TASK 노드를 INVALID 로 전환하여 root cause 수정 강제.
# 무한 루프 방지: upstream_rework_count 컬럼 조건부 ALTER + phase 당 2회 상한.
# ────────────────────────────────────────────────────────────────────────


# upstream artifact 키워드 (downstream QA verdict 텍스트에서 검출).
_UPSTREAM_KEYWORDS: dict[str, list[str]] = {
    "DESIGN": [
        "디자인 시안", "디자인 토큰", "ui 디자인", "디자인 컴포넌트",
        "design token", "design system", "스타일 가이드",
    ],
    "API": [
        "api 설계", "api 명세", "엔드포인트", "endpoint",
        "request schema", "response schema",
    ],
    "DB": [
        "db 설계", "스키마", "테이블 정의", "schema definition",
        "정규화", "외래키", "foreign key",
    ],
    "REQ": [
        "요구사항", "기능 백로그", "유스케이스",
        "requirement", "user story",
    ],
}

UPSTREAM_REWORK_LIMIT_PER_PHASE = 2


async def _ensure_rework_count_column(db: Any) -> None:
    """nodes.upstream_rework_count 컬럼이 없으면 ALTER. idempotent."""
    try:
        await db.execute(
            "ALTER TABLE nodes ADD COLUMN upstream_rework_count INTEGER DEFAULT 0"
        )
    except Exception:
        # 이미 존재 → 무시
        pass


def _classify_upstream_categories(verdict_text: str) -> set[str]:
    """QA verdict 텍스트에서 어느 upstream 카테고리(DESIGN/API/DB/REQ) 가
    원인으로 지목됐는지 분류."""
    text = (verdict_text or "").lower()
    hit: set[str] = set()
    for cat, kws in _UPSTREAM_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                hit.add(cat)
                break
    return hit


async def trigger_upstream_rework_if_needed(
    db: Any,
    failed_qa_node_id: str,
    failed_task_node_id: str,
    qa_verdict_text: str,
) -> int:
    """downstream QA FAIL 시 호출. 사유에 upstream 키워드가 있으면 해당 upstream
    TASK 노드(같은 engagement 의 카테고리 매칭) 를 INVALID 로 전환.

    Returns: 영향받은 upstream 노드 수.
    """
    if db is None or not qa_verdict_text:
        return 0

    categories = _classify_upstream_categories(qa_verdict_text)
    if not categories:
        return 0

    await _ensure_rework_count_column(db)

    # downstream task 가 속한 engagement·phase 조회
    row = await db.fetchone(
        "SELECT engagement_id, phase, dag_id FROM nodes WHERE id=?",
        (failed_task_node_id,),
    )
    if not row:
        return 0
    engagement_id = row["engagement_id"]
    cur_phase = row["phase"]

    # 같은 engagement 의 COMPLETED upstream TASK 중 카테고리 매칭
    # 카테고리 → task_name 키워드 매핑
    cat_to_namekw = {
        "DESIGN": ["디자인", "design"],
        "API": ["api"],
        "DB": ["db", "데이터베이스", "스키마"],
        "REQ": ["요구사항", "백로그", "유스케이스"],
    }
    name_clauses: list[str] = []
    params: list = [engagement_id]
    for cat in categories:
        for kw in cat_to_namekw.get(cat, []):
            name_clauses.append("LOWER(task_name) LIKE ?")
            params.append(f"%{kw.lower()}%")
    if not name_clauses:
        return 0

    sql = (
        "SELECT id, task_name, phase, upstream_rework_count "
        "FROM nodes "
        f"WHERE engagement_id=? AND state='COMPLETED' "
        f"AND node_type='TASK' AND ({' OR '.join(name_clauses)})"
    )
    candidates = await db.fetchall(sql, tuple(params))
    if not candidates:
        return 0

    affected = 0
    now = _now()
    for c in candidates:
        prev_count = int(c.get("upstream_rework_count") or 0)
        if prev_count >= UPSTREAM_REWORK_LIMIT_PER_PHASE:
            logger.info(
                "upstream_rework_skipped node=%s reason=limit count=%d",
                c["id"][:8], prev_count,
            )
            continue
        # 자기 자신 또는 같은 task 는 제외
        if c["id"] == failed_task_node_id:
            continue
        verdict_meta = {
            "upstream_rework": True,
            "triggered_by_qa": failed_qa_node_id,
            "triggered_by_task": failed_task_node_id,
            "categories": sorted(categories),
            "reason_excerpt": qa_verdict_text[:300],
        }
        try:
            await db.execute(
                """UPDATE nodes
                SET state='INVALID',
                    description=?,
                    upstream_rework_count = COALESCE(upstream_rework_count,0)+1,
                    updated_at=?
                WHERE id=? AND state='COMPLETED'""",
                (json.dumps(verdict_meta, ensure_ascii=False), now, c["id"]),
            )
            affected += 1
            logger.info(
                "upstream_rework_invalidated node=%s name=%s cats=%s",
                c["id"][:8], c["task_name"][:30], sorted(categories),
            )
        except Exception as e:
            logger.warning(
                "upstream_rework_failed node=%s err=%s", c["id"][:8], e,
            )

    if affected > 0:
        # observability 에 이벤트 기록
        try:
            from engine.observability.events import log_event
            await log_event(
                db, "upstream_rework_triggered",
                project_id=engagement_id,
                payload={"affected": affected, "categories": sorted(categories),
                         "phase": cur_phase},
            )
        except Exception:
            pass

    return affected
