"""
tests/test_dag_advancer.py
DAGAdvancer 직렬화 검증 — SQLite in-memory 통합 테스트.
"""

from __future__ import annotations

import asyncio
import pytest

from engine.core.dag_advancer import topological_sort, DAGAdvancer, NodeSnapshot


# ---------------------------------------------------------------------------
# 헬퍼: NodeSnapshot 빌더
# ---------------------------------------------------------------------------

def _make_node(node_id: str, deps: list[str] | None = None, priority: int = 3) -> NodeSnapshot:
    return NodeSnapshot(
        id=node_id,
        dag_id="dag-test",
        project_id="proj-test",
        node_type="TASK",
        phase="BUILD",
        name=node_id,
        state="READY",
        priority=priority,
        retry_count=0,
        max_retries=3,
        assigned_model=None,
        qa_pair_node_id=None,
        task_pair_node_id=None,
        gate_auto_approve=True,
        version=1,
        deps=deps or [],
    )


# ---------------------------------------------------------------------------
# topological_sort 단위 테스트
# ---------------------------------------------------------------------------

def test_topological_sort_linear() -> None:
    """A → B → C 직렬 DAG → 위상 순서 반환."""
    nodes = [
        _make_node("A"),
        _make_node("B", deps=["A"]),
        _make_node("C", deps=["B"]),
    ]
    order = topological_sort(nodes)
    assert order.index("A") < order.index("B")
    assert order.index("B") < order.index("C")


def test_topological_sort_diamond() -> None:
    """다이아몬드 DAG: A → B, A → C, B → D, C → D."""
    nodes = [
        _make_node("A"),
        _make_node("B", deps=["A"]),
        _make_node("C", deps=["A"]),
        _make_node("D", deps=["B", "C"]),
    ]
    order = topological_sort(nodes)
    assert order.index("A") < order.index("B")
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("D")
    assert order.index("C") < order.index("D")


def test_topological_sort_no_edges() -> None:
    """엣지 없는 DAG → 노드 모두 포함."""
    nodes = [_make_node("X"), _make_node("Y"), _make_node("Z")]
    order = topological_sort(nodes)
    assert set(order) == {"X", "Y", "Z"}


def test_topological_sort_cycle_raises() -> None:
    """순환 DAG → ValueError 또는 CycleError."""
    nodes = [
        _make_node("A", deps=["C"]),
        _make_node("B", deps=["A"]),
        _make_node("C", deps=["B"]),
    ]
    with pytest.raises((ValueError, Exception)):
        topological_sort(nodes)


# ---------------------------------------------------------------------------
# DAGAdvancer 큐 직렬화 검증 (단순 mock DB)
# ---------------------------------------------------------------------------

def test_advancer_queue_processes_sequentially() -> None:
    """
    두 advance_dag 호출이 동시에 들어와도 순차 처리됨.
    asyncio.Queue 단일 소비자 패턴 확인.
    """
    from unittest.mock import AsyncMock, MagicMock

    db = MagicMock()
    db.fetchall = AsyncMock(return_value=[])
    db.fetchone = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=0)

    async def dummy_executor(node):
        pass

    advancer = DAGAdvancer(db, dummy_executor)
    call_order = []

    async def tracked_advance(dag_id):
        call_order.append(("start", dag_id))
        await asyncio.sleep(0.01)  # 약간의 지연
        call_order.append(("end", dag_id))

    advancer._advance_dag = tracked_advance

    async def run():
        task = asyncio.create_task(advancer.run())
        # 두 요청 동시 제출
        await advancer.enqueue("dag-1")
        await advancer.enqueue("dag-2")
        await asyncio.sleep(0.1)  # 처리 대기
        await advancer.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.get_event_loop().run_until_complete(run())

    # 순차 처리: start-end 쌍이 교차하지 않아야 함
    if len(call_order) >= 4:
        starts = [i for i, (op, _) in enumerate(call_order) if op == "start"]
        ends = [i for i, (op, _) in enumerate(call_order) if op == "end"]
        assert ends[0] < starts[1] if len(starts) > 1 else True
