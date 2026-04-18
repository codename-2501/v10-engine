"""
engine/core/cascade.py
Cascade Invalidation 2-Phase 프로토콜 (v5 신규).
Phase 1: 빠른 마킹 (DB 잠금 최소화) → invalidation_pending=1
Phase 2: 실제 상태 전이 (낙관적 잠금 + 에이전트 종료)
Rule 4: COMPLETED 노드도 상위 변경 시 INVALID 가능
Rule 5: 영향받지 않은 노드는 절대 중단하지 않음
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from engine.core.state_machine import NodeState, StateMachine
from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)

TerminateAgentFn = Callable[[str], Awaitable[None]]


class CascadeInvalidator:
    """
    2-Phase Cascade Invalidation.
    Phase 1: 영향받는 하위 노드들에 invalidation_pending=1 빠른 마킹
    Phase 2: 실제 INVALID 상태 전이 (에이전트 종료 포함)
    """

    # Phase 2 타임아웃 (phase1_queued_at 기준)
    PHASE2_TIMEOUT_SECONDS = 300  # 5분

    def __init__(
        self,
        db: DatabaseAdapter,
        terminate_agent: TerminateAgentFn | None = None,
    ) -> None:
        self._db = db
        self._terminate_agent = terminate_agent

    # ------------------------------------------------------------------
    # Phase 1: 빠른 마킹
    # ------------------------------------------------------------------

    async def phase1_mark_pending(
        self,
        changed_node_id: str,
        change_type: str = "CONTEXTUAL",
        affected_sections: list[str] | None = None,
    ) -> list[str]:
        """
        변경된 노드의 하위(downstream) 노드들에 invalidation_pending=1 마킹.
        DB 잠금 최소화 — 상태 전이 없이 플래그만 설정.
        Rule 5: 영향받는 노드만 마킹 (무관한 노드 절대 터치 안 함).

        Args:
            changed_node_id: 변경된 upstream 노드 ID.
            change_type: "CONTEXTUAL" (전체 재실행) | "PARTIAL" (섹션 패치).
            affected_sections: PARTIAL 시 수정 대상 섹션 목록 (JSON 직렬화 저장).
        반환: 마킹된 node_id 목록.
        """
        import json as _json
        impacted = await self._get_downstream_nodes(changed_node_id)
        if not impacted:
            return []

        now = _now()
        sections_json = _json.dumps(affected_sections or [], ensure_ascii=False)
        for node_id in impacted:
            await self._db.execute(
                """UPDATE nodes
                   SET invalidation_pending=1,
                       invalidation_source_id=?,
                       invalidation_queued_at=?,
                       invalidation_change_type=?,
                       invalidation_affected_sections=?,
                       updated_at=?
                   WHERE id=? AND invalidation_pending=0""",
                (changed_node_id, now, change_type, sections_json, now, node_id),
            )

        logger.info(
            "cascade_phase1_marked changed_node_id=%s impacted_count=%d change_type=%s",
            changed_node_id,
            len(impacted),
            change_type,
        )
        return impacted

    # ------------------------------------------------------------------
    # Phase 2: 실제 상태 전이
    # ------------------------------------------------------------------

    async def phase2_apply_invalid(self, node_id: str) -> bool:
        """
        단일 노드에 INVALID 전이 적용.
        중복 실행 방지: invalidation_pending=0이면 스킵.
        IN_PROGRESS 상태: 에이전트 종료 후 전이.
        반환: 실제로 전이가 적용됐으면 True.
        """
        node = await self._db.fetchone(
            """SELECT id, state, invalidation_pending, version
               FROM nodes WHERE id=?""",
            (node_id,),
        )
        if not node:
            return False

        if node["invalidation_pending"] == 0:
            return False  # 이미 처리됨 또는 대상 아님

        # SKIPPED 노드는 cascade 대상에서 제외
        if node["state"] == NodeState.SKIPPED:
            await self._db.execute(
                "UPDATE nodes SET invalidation_pending=0, updated_at=? WHERE id=?",
                (_now(), node_id),
            )
            return False

        # IN_PROGRESS → 에이전트 종료 먼저
        if node["state"] == NodeState.IN_PROGRESS and self._terminate_agent:
            try:
                await self._terminate_agent(node_id)
            except Exception as exc:
                logger.warning(
                    "cascade_agent_terminate_failed node_id=%s error=%s",
                    node_id,
                    str(exc),
                )

        # 상태 전이 가능 여부 확인
        if not StateMachine.is_valid(node["state"], NodeState.INVALID):
            logger.warning(
                "cascade_invalid_transition_skipped node_id=%s state=%s",
                node_id,
                node["state"],
            )
            # 마킹만 클리어
            await self._db.execute(
                "UPDATE nodes SET invalidation_pending=0, updated_at=? WHERE id=?",
                (_now(), node_id),
            )
            return False

        # 낙관적 잠금으로 INVALID 전이
        affected = await self._db.execute(
            """UPDATE nodes
               SET state='INVALID',
                   invalidation_pending=0,
                   updated_at=?,
                   version=version+1
               WHERE id=? AND version=?""",
            (_now(), node_id, node["version"]),
        )

        if affected == 0:
            logger.warning("cascade_phase2_version_conflict node_id=%s", node_id)
            return False

        logger.info(
            "cascade_phase2_applied node_id=%s from_state=%s",
            node_id,
            node["state"],
        )
        return True

    async def process_pending_batch(self, limit: int = 50) -> int:
        """
        invalidation_pending=1인 노드를 배치로 Phase 2 적용.
        ShutdownManager 또는 백그라운드 태스크에서 주기적 호출.
        반환: 처리된 노드 수.
        """
        rows = await self._db.fetchall(
            """SELECT id FROM nodes
               WHERE invalidation_pending=1
               ORDER BY invalidation_queued_at ASC
               LIMIT ?""",
            (limit,),
        )
        count = 0
        for row in rows:
            applied = await self.phase2_apply_invalid(row["id"])
            if applied:
                count += 1

        if count:
            logger.info("cascade_batch_processed count=%d", count)
        return count

    # ------------------------------------------------------------------
    # 타임아웃 노드 강제 처리
    # ------------------------------------------------------------------

    async def cleanup_timed_out(self) -> int:
        """
        Phase 2 타임아웃(5분) 초과 pending 노드 강제 클리어.
        반환: 처리된 수.
        """
        cutoff = datetime.now(timezone.utc).isoformat()
        rows = await self._db.fetchall(
            """SELECT id FROM nodes
               WHERE invalidation_pending=1
                 AND invalidation_queued_at < datetime(?, '-5 minutes')""",
            (cutoff,),
        )
        count = 0
        for row in rows:
            await self.phase2_apply_invalid(row["id"])
            count += 1
        return count

    # ------------------------------------------------------------------
    # 하위 노드 탐색 (BFS)
    # ------------------------------------------------------------------

    async def _get_downstream_nodes(self, node_id: str) -> list[str]:
        """
        edges 테이블 기반 BFS로 downstream 노드 전체 탐색.
        Rule 5: 직접/간접 의존 노드만 포함.
        """
        visited: set[str] = set()
        queue = [node_id]
        result: list[str] = []

        while queue:
            current = queue.pop(0)
            rows = await self._db.fetchall(
                "SELECT to_node_id FROM edges WHERE from_node_id=? AND is_active=1",
                (current,),
            )
            for r in rows:
                child = r["to_node_id"]
                if child not in visited:
                    visited.add(child)
                    result.append(child)
                    queue.append(child)

        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
