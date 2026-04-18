"""
engine/core/dag_advancer.py
DAGAdvancer — asyncio.Queue 기반 직렬 advance.
핵심: 단일 소비자 루프 → 경쟁 조건(Race Condition) 없음.
Rule 2: 위상 정렬 실행 순서만 허용 — 임의 순서 금지.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from engine.core.state_machine import NodeState, NodeType, Phase, StateMachine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class NodeSnapshot:
    """DB rows의 인메모리 표현."""
    id: str
    dag_id: str
    project_id: str
    node_type: str
    phase: str
    name: str
    state: str
    priority: int
    retry_count: int
    max_retries: int
    assigned_model: str | None
    qa_pair_node_id: str | None
    task_pair_node_id: str | None
    gate_auto_approve: bool
    version: int
    deps: list[str] = field(default_factory=list)   # 선행 노드 ID 목록


@dataclass
class AdvanceResult:
    dag_id: str
    launched: list[str] = field(default_factory=list)    # 실행 시작된 node_id
    blocked: list[str] = field(default_factory=list)     # BLOCKED 상태로 전환된 node_id
    gate_pending: list[str] = field(default_factory=list) # AWAITING_APPROVAL 상태
    completed: bool = False                               # DAG 전체 완료 여부


# ---------------------------------------------------------------------------
# 위상 정렬 (Kahn's Algorithm — Rule 1: Cycle 절대 불가)
# ---------------------------------------------------------------------------

def topological_sort(nodes: list[NodeSnapshot]) -> list[str]:
    """
    Kahn's Algorithm으로 위상 정렬.
    순환 참조 감지 시 ValueError 발생 (Rule 1 위반).
    반환: node_id 순서 리스트
    """
    in_degree: dict[str, int] = {n.id: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)

    for node in nodes:
        for dep_id in node.deps:
            adj[dep_id].append(node.id)
            in_degree[node.id] = in_degree.get(node.id, 0) + 1

    # 진입 차수 0인 노드부터 시작 (priority 낮을수록 우선)
    queue: deque[str] = deque(
        sorted(
            [nid for nid, deg in in_degree.items() if deg == 0],
            key=lambda nid: next((n.priority for n in nodes if n.id == nid), 3),
        )
    )
    order: list[str] = []

    while queue:
        nid = queue.popleft()
        order.append(nid)
        for successor in adj[nid]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(order) != len(nodes):
        raise ValueError(
            f"DAG 순환 참조 감지 (Rule 1 위반): "
            f"{len(nodes) - len(order)}개 노드 처리 불가"
        )

    return order


# ---------------------------------------------------------------------------
# NodeExecutor 타입 (외부 주입)
# ---------------------------------------------------------------------------

NodeExecutorFn = Callable[[NodeSnapshot], Awaitable[None]]


# ---------------------------------------------------------------------------
# DAGAdvancer
# ---------------------------------------------------------------------------

class DAGAdvancer:
    """
    단일 소비자 Queue 기반 DAG 진행 엔진.
    - enqueue(dag_id) → Queue에 추가
    - run() → 단일 루프에서 순서대로 처리 (절대 병렬 advance 없음)
    - _advance_dag(): READY 노드 탐색 → 실행 위임 → 상태 전이
    """

    # 동시 실행 노드 최대 수 (API 과부하 방지)
    MAX_CONCURRENT_NODES = 5

    def __init__(
        self,
        db,                          # DatabaseAdapter 인스턴스
        executor: NodeExecutorFn,    # 노드 실행 함수 (외부 주입)
    ) -> None:
        self._db = db
        self._executor = executor
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running: bool = False
        self._shutdown_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_NODES)

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    async def enqueue(self, dag_id: str) -> None:
        """DAG advance 요청을 큐에 추가."""
        await self._queue.put(dag_id)

    async def run(self) -> None:
        """
        단일 소비자 루프.
        ShutdownManager가 stop()을 호출할 때까지 실행.
        """
        self._running = True
        logger.info("DAGAdvancer started")
        while self._running:
            try:
                dag_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._advance_dag(dag_id)
            except Exception as exc:
                logger.error(
                    "dag_advance_failed dag_id=%s error=%s",
                    dag_id,
                    str(exc),
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

        logger.info("DAGAdvancer stopped")

    async def stop(self) -> None:
        """Graceful stop — 현재 처리 중인 dag_id 완료 후 종료."""
        self._running = False
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # DAG 진행 로직
    # ------------------------------------------------------------------

    async def _advance_dag(self, dag_id: str) -> AdvanceResult:
        """
        DAG 상태를 한 스텝 전진.
        1. READY 상태 노드 확인 → 실행 가능 여부 검사
        2. 의존성 모두 COMPLETED → READY 전환
        3. Gate 노드 처리
        4. DAG 완료 여부 판단
        """
        result = AdvanceResult(dag_id=dag_id)

        # PAUSED 상태면 새 노드 실행 차단 (진행중 노드는 마무리 허용)
        dag_row = await self._db.fetchone("SELECT status FROM dags WHERE id=?", (dag_id,))
        if not dag_row or dag_row["status"] == "PAUSED":
            logger.info("dag_paused_skip dag_id=%s", dag_id)
            return result

        # 노드 + 엣지 로드
        nodes = await self._load_nodes(dag_id)
        await asyncio.sleep(0)  # 대규모 DAG 로드 후 이벤트 루프 양보
        if not nodes:
            logger.warning("dag_no_nodes dag_id=%s", dag_id)
            return result

        node_map = {n.id: n for n in nodes}

        # 위상 정렬 검증 (Rule 1: Cycle 감지)
        try:
            topo_order = topological_sort(nodes)
        except ValueError as exc:
            logger.error("dag_cycle_detected dag_id=%s error=%s", dag_id, str(exc))
            await self._mark_dag_corrupted(dag_id)
            return result

        await asyncio.sleep(0)  # CPU 집약적 topological_sort 후 이벤트 루프 양보

        # topo_order 저장 (DB 캐시)
        await self._save_topo_order(dag_id, topo_order)

        # 현재 실행 중인 노드 수 계산 (한 사이클에서 MAX 초과 launch 방지)
        _in_progress_count = sum(
            1 for n in node_map.values() if n.state == NodeState.IN_PROGRESS
        )

        # 각 NOT_STARTED/BLOCKED 노드를 위상 순서대로 평가
        for _i, node_id in enumerate(topo_order):
            if _i % 200 == 0 and _i > 0:
                await asyncio.sleep(0)  # 대규모 DAG에서 주기적 이벤트 루프 양보
            node = node_map[node_id]

            _DONE = {NodeState.COMPLETED, NodeState.SKIPPED}

            if node.state in {
                NodeState.IN_PROGRESS, NodeState.COMPLETED, NodeState.SKIPPED,
                NodeState.AWAITING_APPROVAL, NodeState.NEEDS_HUMAN,
                NodeState.SUSPENDED,
            }:
                continue  # 이미 처리된 상태

            # FAILED 노드: retry_count < max_retries 이면 자동 복구
            if node.state == NodeState.FAILED:
                if node.retry_count < node.max_retries:
                    deps_ok = all(
                        node_map[d].state in _DONE
                        for d in node.deps if d in node_map
                    )
                    if deps_ok:
                        await self._transition_node(node, NodeState.READY)
                        await self._launch_node(node, result)
                        logger.info(
                            "node_auto_retry node_id=%s retry=%d/%d",
                            node.id, node.retry_count, node.max_retries,
                        )
                continue

            deps_states = [node_map[d].state for d in node.deps if d in node_map]
            all_completed = all(s in _DONE for s in deps_states)
            any_failed = any(
                s in {NodeState.FAILED, NodeState.NEEDS_HUMAN, NodeState.INVALID}
                for s in deps_states
            )

            if any_failed:
                if node.state != NodeState.BLOCKED:
                    await self._transition_node(node, NodeState.BLOCKED)
                result.blocked.append(node_id)

            elif all_completed:
                if node.node_type == NodeType.GATE:
                    await self._handle_gate(node, result)
                elif node.state == NodeState.READY:
                    # 이미 READY (dag/start에서 설정됨) → 바로 launch
                    if _in_progress_count < self.MAX_CONCURRENT_NODES:
                        await self._launch_node(node, result)
                        _in_progress_count += 1
                elif node.state in {NodeState.NOT_STARTED, NodeState.BLOCKED, NodeState.INVALID}:
                    await self._transition_node(node, NodeState.READY)
                    if _in_progress_count < self.MAX_CONCURRENT_NODES:
                        await self._launch_node(node, result)
                        _in_progress_count += 1
            else:
                if node.state in {NodeState.NOT_STARTED, NodeState.READY, NodeState.INVALID}:
                    await self._transition_node(node, NodeState.BLOCKED)
                    result.blocked.append(node_id)

        # DAG 완료 여부
        if all(
            n.state in {NodeState.COMPLETED, NodeState.SKIPPED}
            for n in nodes
            if n.node_type != NodeType.GATE
        ):
            result.completed = True
            await self._mark_dag_completed(dag_id)
            logger.info("dag_completed dag_id=%s", dag_id)

        return result

    async def _launch_node(self, node: NodeSnapshot, result: AdvanceResult) -> None:
        """READY → IN_PROGRESS + executor 호출."""
        await self._transition_node(node, NodeState.IN_PROGRESS)
        result.launched.append(node.id)
        # executor는 비동기이므로 태스크로 생성 (advance 루프 블로킹 방지)
        asyncio.create_task(
            self._run_node_safe(node),
            name=f"node-{node.id[:8]}",
        )

    async def _run_node_safe(self, node: NodeSnapshot) -> None:
        """executor 실행 래퍼 — 예외 캡처 + 자동 재시도 + FAILED 전이."""
        async with self._semaphore:  # 동시 실행 수 제한
            await self._run_node_safe_inner(node)

    async def _run_node_safe_inner(self, node: NodeSnapshot) -> None:
        """실제 실행 로직."""
        try:
            await self._executor(node)
            # 성공: 실행 완료 후 DAG 재진행 (하위 노드 READY 처리)
            await self.enqueue(node.dag_id)
        except Exception as exc:
            error_str = str(exc)

            # API 한도 초과: 재시도 의미 없음 → SUSPENDED 처리
            if "hit your limit" in error_str or "You've hit your limit" in error_str:
                logger.warning(
                    "node_api_limit_suspended node_id=%s — suspending until limit resets",
                    node.id,
                )
                try:
                    await self._db.execute(
                        "UPDATE nodes SET state='SUSPENDED', suspension_reason=?, updated_at=? WHERE id=?",
                        ("API 한도 초과 — 자동 재개 대기", _now(), node.id),
                    )
                except Exception:
                    pass
                return  # DAG 재큐 안 함 (한도 리셋 후 watchdog/수동 재개)

            new_retry = node.retry_count + 1
            logger.error(
                "node_executor_failed node_id=%s error=%s retry=%d/%d",
                node.id, str(exc), new_retry, node.max_retries,
                exc_info=True,
            )

            # retry_count 증가 + 실패 사유 기록 (DB 업데이트)
            error_msg = str(exc)[:500]
            try:
                import json as _json
                existing = await self._db.fetchone(
                    "SELECT failure_reasons FROM nodes WHERE id=?", (node.id,)
                )
                reasons = _json.loads(existing["failure_reasons"] or "[]") if existing else []
                reasons.append({
                    "attempt": new_retry,
                    "reason": error_msg,
                    "at": _now(),
                })
                await self._db.execute(
                    "UPDATE nodes SET retry_count=?, failure_reasons=?, updated_at=? WHERE id=?",
                    (new_retry, _json.dumps(reasons, ensure_ascii=False), _now(), node.id),
                )
            except Exception:
                await self._db.execute(
                    "UPDATE nodes SET retry_count=?, updated_at=? WHERE id=?",
                    (new_retry, _now(), node.id),
                )
            node.retry_count = new_retry

            if new_retry < node.max_retries:
                # 재시도 가능: IN_PROGRESS → READY (재실행 대기)
                try:
                    await self._transition_node(node, NodeState.READY)
                    logger.info(
                        "node_retry_scheduled node_id=%s retry=%d/%d",
                        node.id, new_retry, node.max_retries,
                    )
                except Exception:
                    # READY 전이 실패 시 FAILED로 폴백
                    await self._transition_node(node, NodeState.FAILED)
                # 지수 백오프 후 DAG 재큐 (즉시 재시도 방지)
                await asyncio.sleep(min(2 ** new_retry, 30))
                await self.enqueue(node.dag_id)
            else:
                # 최대 재시도 초과: FAILED 확정
                try:
                    await self._transition_node(node, NodeState.FAILED)
                except Exception as t_exc:
                    logger.error(
                        "node_failed_transition_error node_id=%s error=%s",
                        node.id, str(t_exc),
                    )
                logger.warning(
                    "node_max_retries_exhausted node_id=%s retries=%d",
                    node.id, new_retry,
                )
                # 하위 노드들이 BLOCKED 처리되도록 DAG 재큐
                await self.enqueue(node.dag_id)

    async def _handle_gate(self, node: NodeSnapshot, result: AdvanceResult) -> None:
        """Gate 노드 처리: READY → IN_PROGRESS → COMPLETED(auto) 또는 AWAITING_APPROVAL(수동)."""
        if node.state == NodeState.SKIPPED:
            return
        # NOT_STARTED/BLOCKED → READY → IN_PROGRESS 순차 전이
        if node.state in {NodeState.NOT_STARTED, NodeState.BLOCKED, NodeState.INVALID}:
            await self._transition_node(node, NodeState.READY)
        if node.state == NodeState.READY:
            await self._transition_node(node, NodeState.IN_PROGRESS)

        if node.state != NodeState.IN_PROGRESS:
            return  # 이미 처리된 상태

        if node.gate_auto_approve:
            await self._transition_node(node, NodeState.COMPLETED)
            logger.info("gate_auto_approved node_id=%s", node.id)
        else:
            await self._transition_node(node, NodeState.AWAITING_APPROVAL)
            result.gate_pending.append(node.id)
            logger.info("gate_awaiting_approval node_id=%s", node.id)

    # ------------------------------------------------------------------
    # DB 조작 헬퍼
    # ------------------------------------------------------------------

    async def _load_nodes(self, dag_id: str) -> list[NodeSnapshot]:
        """nodes + edges를 JOIN하여 NodeSnapshot 목록 반환."""
        rows = await self._db.fetchall(
            """SELECT n.id, n.dag_id, n.project_id, n.node_type, n.phase,
                      n.name, n.state, n.priority, n.retry_count, n.max_retries,
                      n.assigned_model, n.qa_pair_node_id, n.task_pair_node_id,
                      n.gate_auto_approve, n.version
               FROM nodes n
               WHERE n.dag_id = ?
               ORDER BY n.priority, n.created_at""",
            (dag_id,),
        )

        edge_rows = await self._db.fetchall(
            """SELECT from_node_id, to_node_id FROM edges
               WHERE dag_id = ? AND is_active = 1""",
            (dag_id,),
        )

        # 노드별 선행 의존 목록 구성
        deps_map: dict[str, list[str]] = defaultdict(list)
        for e in edge_rows:
            deps_map[e["to_node_id"]].append(e["from_node_id"])

        snapshots = []
        for r in rows:
            snapshots.append(NodeSnapshot(
                id=r["id"],
                dag_id=r["dag_id"],
                project_id=r["project_id"],
                node_type=r["node_type"],
                phase=r["phase"],
                name=r["name"],
                state=r["state"],
                priority=r["priority"],
                retry_count=r["retry_count"],
                max_retries=r["max_retries"],
                assigned_model=r["assigned_model"],
                qa_pair_node_id=r["qa_pair_node_id"],
                task_pair_node_id=r["task_pair_node_id"],
                gate_auto_approve=bool(r["gate_auto_approve"]),
                version=r["version"],
                deps=deps_map.get(r["id"], []),
            ))
        return snapshots

    async def _transition_node(self, node: NodeSnapshot, to_state: str) -> None:
        """상태 전이 검증 + DB 업데이트 (낙관적 잠금 + 최대 3회 재시도)."""
        for attempt in range(3):
            StateMachine.transition(node.state, to_state, node.id)  # 유효성 검증
            affected = await self._db.execute(
                """UPDATE nodes SET state=?, updated_at=?, version=version+1
                   WHERE id=? AND version=?""",
                (to_state, _now(), node.id, node.version),
            )
            if affected > 0:
                node.state = to_state
                node.version += 1
                logger.info(
                    "node_state_changed node_id=%s from=%s to=%s",
                    node.id, node.state, to_state,
                )
                return

            # 낙관적 잠금 충돌 → DB 최신 상태 리로드
            fresh = await self._db.fetchone(
                "SELECT state, version FROM nodes WHERE id=?", (node.id,)
            )
            if not fresh:
                logger.error("node_not_found_on_conflict node_id=%s", node.id)
                return

            # 이미 목표 상태 도달 (다른 태스크가 먼저 전환)
            if fresh["state"] == to_state:
                node.state = fresh["state"]
                node.version = fresh["version"]
                return

            # 새 상태에서 목표 전이가 유효하지 않으면 중단 (정상 경쟁 조건)
            if not StateMachine.is_valid(fresh["state"], to_state):
                logger.warning(
                    "node_transition_invalid_after_conflict "
                    "node_id=%s fresh=%s to=%s — skipping",
                    node.id, fresh["state"], to_state,
                )
                node.state = fresh["state"]
                node.version = fresh["version"]
                return

            # 다음 시도를 위해 노드 상태 동기화
            node.state = fresh["state"]
            node.version = fresh["version"]
            logger.warning(
                "node_transition_conflict node_id=%s attempt=%d retrying",
                node.id, attempt + 1,
            )
            await asyncio.sleep(0.05 * (attempt + 1))

        logger.warning(
            "node_transition_max_retries node_id=%s to=%s — giving up",
            node.id, to_state,
        )

    async def _save_topo_order(self, dag_id: str, order: list[str]) -> None:
        await self._db.execute(
            "UPDATE dags SET topo_order=?, updated_at=? WHERE id=?",
            (json.dumps(order), _now(), dag_id),
        )

    async def _mark_dag_completed(self, dag_id: str) -> None:
        await self._db.execute(
            "UPDATE dags SET status='COMPLETED', updated_at=?, version=version+1 WHERE id=?",
            (_now(), dag_id),
        )

    async def _mark_dag_corrupted(self, dag_id: str) -> None:
        await self._db.execute(
            "UPDATE dags SET status='CORRUPTED', updated_at=?, version=version+1 WHERE id=?",
            (_now(), dag_id),
        )


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
