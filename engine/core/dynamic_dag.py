"""
engine/core/dynamic_dag.py
Phase F-2: 런타임 DAG 노드 주입.

주입 시나리오:
- 노드 실패 → 수리 노드(REPAIR) 주입 → 실패 노드 재실행
- 런타임 분석 결과 → 추가 분석 노드 주입
- 외부 이벤트 → 반응 노드 주입

핵심 안전장치:
- MAX_INJECTIONS_PER_DAG=20 (재귀 폭발 방지)
- injected_by 체인 루프 감지 (A→B→A 차단)
- NOT_STARTED 상태로 삽입 → DAGAdvancer가 topological 처리

코어 5개 파일은 변경하지 않고 DAGAdvancer.enqueue() 만 호출.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InjectionError(Exception):
    """노드 주입 실패 (한계 초과, 루프, DB 에러 등)."""


@dataclass
class NodeSpec:
    """주입할 노드의 스펙."""
    name: str
    node_type: str = "TASK"           # 'TASK' | 'QA' | 'GATE'
    phase: str = "BUILD"
    priority: int = 3
    max_retries: int = 3
    assigned_model: str | None = None
    gate_auto_approve: bool = False
    description: str = ""
    gate_trigger_type: str = "MANUAL_DESIGNER"
    metadata: dict = field(default_factory=dict)


class DynamicDAGExtension:
    """
    런타임 노드 주입 매니저.

    사용:
      ext = DynamicDAGExtension(db, dag_advancer.enqueue)
      node_id = await ext.inject_node(
          dag_id, parent_node_id, NodeSpec(name="repair-auth"),
          child_node_ids=[original_failed_node_id],
          injected_by=trigger_node_id,
      )
    """

    MAX_INJECTIONS_PER_DAG = 20
    MAX_LOOP_HOPS = 20

    def __init__(
        self,
        db: Any,
        enqueue_fn: Callable[[str], Awaitable[None]],
    ) -> None:
        self._db = db
        self._enqueue = enqueue_fn

    # ── 공개 API ──────────────────────────────────────────────

    async def inject_node(
        self,
        dag_id: str,
        parent_node_id: str,
        node_spec: NodeSpec,
        child_node_ids: list[str] | None = None,
        injected_by: str | None = None,
    ) -> str:
        """
        새 노드를 DAG에 주입.
        parent_node_id → new_node → each(child_node_ids) 엣지 생성.
        반환: 새 노드 ID.
        실패 시 InjectionError.
        """
        child_node_ids = child_node_ids or []

        # 1. DAG 유효성 확인
        dag_row = await self._db.fetchone(
            "SELECT project_id, status FROM dags WHERE id=?", (dag_id,)
        )
        if not dag_row:
            raise InjectionError(f"dag not found: {dag_id}")
        project_id = dag_row["project_id"]

        # 2. 주입 한계 체크
        count = await self._count_injected_nodes(dag_id)
        if count >= self.MAX_INJECTIONS_PER_DAG:
            raise InjectionError(
                f"max injections exceeded: {count}/{self.MAX_INJECTIONS_PER_DAG}"
            )

        # 3. 루프 감지 (injected_by 체인 역추적)
        if injected_by:
            if await self._detects_injection_loop(
                injected_by_candidate=injected_by,
                new_parent=parent_node_id,
            ):
                raise InjectionError(
                    f"injection loop detected via injected_by={injected_by[:8]}"
                )

        # 4. 부모 노드 확인
        parent_row = await self._db.fetchone(
            "SELECT id FROM nodes WHERE id=? AND dag_id=?",
            (parent_node_id, dag_id),
        )
        if not parent_row:
            raise InjectionError(
                f"parent node not in dag: {parent_node_id[:8]}"
            )

        # 5. 자식 노드 유효성
        for cid in child_node_ids:
            crow = await self._db.fetchone(
                "SELECT id FROM nodes WHERE id=? AND dag_id=?",
                (cid, dag_id),
            )
            if not crow:
                raise InjectionError(
                    f"child node not in dag: {cid[:8]}"
                )

        # 6. 노드 INSERT
        new_id = str(uuid.uuid4())
        now = _now()
        try:
            await self._db.execute(
                """INSERT INTO nodes (
                    id, dag_id, project_id, node_type, phase, name,
                    description, state, gate_auto_approve, gate_trigger_type,
                    assigned_model, retry_count, max_retries,
                    priority, version, injected_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'NOT_STARTED', ?, ?,
                         ?, 0, ?, ?, 0, ?, ?, ?)""",
                (
                    new_id, dag_id, project_id,
                    node_spec.node_type, node_spec.phase, node_spec.name,
                    node_spec.description,
                    1 if node_spec.gate_auto_approve else 0,
                    node_spec.gate_trigger_type,
                    node_spec.assigned_model,
                    node_spec.max_retries,
                    node_spec.priority,
                    injected_by,
                    now, now,
                ),
            )
        except Exception as e:
            raise InjectionError(f"node insert failed: {e}") from e

        # 7. 엣지 INSERT (parent → new → each child)
        try:
            edge_now = _now()
            # parent → new
            await self._db.execute(
                """INSERT INTO edges (id, dag_id, from_node_id, to_node_id,
                                      edge_type, is_active, weight, created_at)
                   VALUES (?, ?, ?, ?, 'DEPENDS_ON', 1, 1, ?)""",
                (str(uuid.uuid4()), dag_id, parent_node_id, new_id, edge_now),
            )
            # new → each child
            for cid in child_node_ids:
                await self._db.execute(
                    """INSERT INTO edges (id, dag_id, from_node_id, to_node_id,
                                          edge_type, is_active, weight, created_at)
                       VALUES (?, ?, ?, ?, 'DEPENDS_ON', 1, 1, ?)""",
                    (str(uuid.uuid4()), dag_id, new_id, cid, edge_now),
                )
        except Exception as e:
            raise InjectionError(f"edge insert failed: {e}") from e

        logger.info(
            "node_injected dag=%s new=%s parent=%s children=%d by=%s name=%s",
            dag_id[:8], new_id[:8], parent_node_id[:8],
            len(child_node_ids),
            (injected_by or "none")[:8],
            node_spec.name[:50],
        )

        # 8. DAGAdvancer 재큐 (코어 접점 — 유일)
        try:
            await self._enqueue(dag_id)
        except Exception as e:
            logger.warning(
                "enqueue_after_inject_failed dag=%s error=%s",
                dag_id[:8], e,
            )

        return new_id

    async def inject_repair_node(
        self,
        dag_id: str,
        failed_node_id: str,
        repair_hint: str = "",
    ) -> str:
        """
        실패 노드의 수리 노드 주입 헬퍼.
        실패 노드의 phase/name 기반으로 자동 스펙 생성.
        """
        failed_row = await self._db.fetchone(
            "SELECT name, phase, project_id FROM nodes WHERE id=? AND dag_id=?",
            (failed_node_id, dag_id),
        )
        if not failed_row:
            raise InjectionError(
                f"failed node not in dag: {failed_node_id[:8]}"
            )

        spec = NodeSpec(
            name=f"repair-{failed_row['name'][:50]}",
            node_type="TASK",
            phase=failed_row["phase"],
            priority=5,  # 긴급
            max_retries=2,
            description=(
                f"Auto-repair for failed node {failed_node_id[:8]}. "
                f"Hint: {repair_hint[:300]}"
            ),
            metadata={"repair_for": failed_node_id},
        )

        # 부모: 실패 노드와 같은 조상 (실패 노드의 부모)
        # 자식: 실패 노드 (재실행되도록)
        ancestor = await self._db.fetchone(
            """SELECT from_node_id FROM edges
               WHERE dag_id=? AND to_node_id=? AND is_active=1
               LIMIT 1""",
            (dag_id, failed_node_id),
        )
        parent_id = (
            ancestor["from_node_id"] if ancestor else failed_node_id
        )

        return await self.inject_node(
            dag_id=dag_id,
            parent_node_id=parent_id,
            node_spec=spec,
            child_node_ids=[failed_node_id],
            injected_by=failed_node_id,
        )

    # ── 내부 유틸 ─────────────────────────────────────────────

    async def _count_injected_nodes(self, dag_id: str) -> int:
        try:
            row = await self._db.fetchone(
                """SELECT COUNT(*) AS cnt FROM nodes
                   WHERE dag_id=? AND injected_by IS NOT NULL""",
                (dag_id,),
            )
            return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.warning("count_injected_failed dag=%s: %s", dag_id[:8], e)
            return 0

    async def _detects_injection_loop(
        self,
        injected_by_candidate: str,
        new_parent: str,
    ) -> bool:
        """
        injected_by_candidate 의 injected_by 체인을 역추적하며 new_parent가
        나타나면 루프로 판정. MAX_LOOP_HOPS 홉 이내.
        """
        current = injected_by_candidate
        for _ in range(self.MAX_LOOP_HOPS):
            if current == new_parent:
                return True
            row = await self._db.fetchone(
                "SELECT injected_by FROM nodes WHERE id=?", (current,)
            )
            if not row or not row["injected_by"]:
                return False
            current = row["injected_by"]
        # 홉 한계 도달 — 루프로 간주 (방어)
        logger.warning(
            "loop_check_max_hops_reached candidate=%s",
            injected_by_candidate[:8],
        )
        return True
