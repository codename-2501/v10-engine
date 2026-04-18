"""
tests/test_cascade.py
CascadeInvalidator 2-Phase 통합 테스트.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, call

from engine.core.cascade import CascadeInvalidator


def _make_db(
    downstream_ids: list[str] | None = None,
    pending_nodes: list[dict] | None = None,
) -> MagicMock:
    db = MagicMock()

    async def fetchall(query, params=()):
        q = query.lower()
        if "invalidation_pending" in q:
            return pending_nodes or []
        if "from edges" in q or "to_node_id" in q:
            # downstream nodes — to_node_id 키로 반환
            return [{"to_node_id": nid} for nid in (downstream_ids or [])]
        return []

    async def fetchone(query, params=()):
        if params and params[0] in (downstream_ids or []):
            return {"state": "COMPLETED", "version": 1, "id": params[0],
                    "invalidation_pending": 0}
        return None

    db.fetchall = AsyncMock(side_effect=fetchall)
    db.fetchone = AsyncMock(side_effect=fetchone)
    db.execute = AsyncMock(return_value=1)
    return db


def test_phase1_marks_downstream_pending() -> None:
    """Phase1: 하위 노드들을 invalidation_pending=1 로 마킹."""
    downstream = ["node-B", "node-C"]
    db = _make_db(downstream_ids=downstream)
    invalidator = CascadeInvalidator(db)

    asyncio.get_event_loop().run_until_complete(
        invalidator.phase1_mark_pending("node-A")
    )

    # execute 호출에 UPDATE + invalidation_pending 포함
    calls_sql = [str(c) for c in db.execute.call_args_list]
    assert any("invalidation_pending" in s for s in calls_sql)


def test_phase2_applies_invalid_transition() -> None:
    """Phase2: pending 노드에 INVALID 전이."""
    pending = [
        {"id": "node-B", "state": "COMPLETED", "version": 1, "invalidation_pending": 1},
    ]
    db = _make_db(pending_nodes=pending)

    async def fetchone(query, params=()):
        for pn in pending:
            if params and pn["id"] in str(params):
                return pn
        return None

    db.fetchone = AsyncMock(side_effect=fetchone)
    invalidator = CascadeInvalidator(db)

    asyncio.get_event_loop().run_until_complete(
        invalidator.phase2_apply_invalid("node-B")
    )

    # INVALID UPDATE 호출 확인
    calls_sql = [str(c) for c in db.execute.call_args_list]
    assert any("INVALID" in s for s in calls_sql)


def test_empty_downstream_no_ops() -> None:
    """하위 노드가 없으면 아무 작업도 하지 않음."""
    db = _make_db(downstream_ids=[])
    db.fetchall = AsyncMock(return_value=[])
    invalidator = CascadeInvalidator(db)

    asyncio.get_event_loop().run_until_complete(
        invalidator.phase1_mark_pending("node-A")
    )
    # execute가 호출되지 않아야 함
    db.execute.assert_not_called()
