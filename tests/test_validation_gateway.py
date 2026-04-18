"""
tests/test_validation_gateway.py
ValidationGateway C1~C9 전체 케이스 테스트.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _db(fetchone_val=None, fetchall_val=None, execute_val=1):
    db = MagicMock()
    db.fetchone = AsyncMock(return_value=fetchone_val)
    db.fetchall = AsyncMock(return_value=fetchall_val or [])
    db.execute = AsyncMock(return_value=execute_val)
    return db


# ── C1 (이미 test_state_machine.py에서 커버) 재확인 ──────────────────────

def test_c1_no_cycle():
    from engine.core.validation_gateway import ValidationGateway
    db = _db(fetchall_val=[
        {"from_node_id": "A", "to_node_id": "B"},
        {"from_node_id": "B", "to_node_id": "C"},
    ])
    gw = ValidationGateway(db)
    asyncio.get_event_loop().run_until_complete(gw.check_c1_no_cycle("dag-1"))


# ── C3: INVALID deps → BLOCKED ───────────────────────────────────────────

def test_c3_blocks_node():
    from engine.core.validation_gateway import ValidationGateway
    # invalid deps 있음
    db = MagicMock()
    db.fetchall = AsyncMock(return_value=[{"state": "INVALID"}])
    db.fetchone = AsyncMock(return_value={"state": "READY", "version": 1})
    db.execute = AsyncMock(return_value=1)
    gw = ValidationGateway(db)
    result = asyncio.get_event_loop().run_until_complete(gw.check_c3_deps_invalid("node-B"))
    assert result is True


# ── C6: 버전 충돌 ─────────────────────────────────────────────────────────

def test_c6_raises_on_version_mismatch():
    from engine.core.validation_gateway import ValidationGateway, GateConflictError
    db = _db(fetchone_val={"version": 5})
    gw = ValidationGateway(db)
    with pytest.raises(GateConflictError) as exc_info:
        asyncio.get_event_loop().run_until_complete(gw.check_c6_version("node-1", 3))
    assert exc_info.value.details["current_version"] == 5


def test_c6_passes_on_correct_version():
    from engine.core.validation_gateway import ValidationGateway
    db = _db(fetchone_val={"version": 3})
    gw = ValidationGateway(db)
    asyncio.get_event_loop().run_until_complete(gw.check_c6_version("node-1", 3))


# ── C7: 예산 사전 차단 ────────────────────────────────────────────────────

def test_c7_passes_when_budget_ok():
    from engine.core.validation_gateway import ValidationGateway

    async def fetchone_side(q, p=()):
        if "node_budget_overrides" in q:
            return None  # 오버라이드 없음
        if "agent_token_usage" in q or "sum(" in q.lower():
            return {"total": 0}
        return None

    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone_side)
    db.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=1)

    gw = ValidationGateway(db)
    # 짧은 프롬프트 → 예산 내
    asyncio.get_event_loop().run_until_complete(
        gw.check_c7_budget_preemption(
            "node-1", "sonnet", "Hello", "proj-1", "PLANNING"
        )
    )


def test_c7_suspends_on_huge_prompt():
    from engine.core.validation_gateway import ValidationGateway, BudgetPreemptionError

    async def fetchone_side(q, p=()):
        if "node_budget_overrides" in q:
            return None
        if "agent_token_usage" in q or "sum(" in q.lower():
            return {"total": 0}
        return {"state": "READY", "version": 1}

    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone_side)
    db.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=1)

    gw = ValidationGateway(db)
    huge = "你好世界 " * 60000  # CJK(중국어) × 1.7 → 추정치 초과
    with pytest.raises(BudgetPreemptionError):
        asyncio.get_event_loop().run_until_complete(
            gw.check_c7_budget_preemption(
                "node-1", "sonnet", huge, "proj-1", "PLANNING"
            )
        )


# ── C8: 헌법 활성화 ───────────────────────────────────────────────────────

def test_c8_no_conflict_transitions_immediately():
    from engine.core.validation_gateway import ValidationGateway

    async def fetchone_side(q, p=()):
        if "constitution_versions" in q:
            if p and p[0] == "cv_v1":
                return {"id": "cv_v1", "version": "v1.0", "rules_hash": "aaa", "file_path": "/nonexistent_v1.md"}
            if p and p[0] == "cv_v2":
                return {"id": "cv_v2", "version": "v2.0", "rules_hash": "aaa", "file_path": "/nonexistent_v2.md"}
        if "projects" in q:
            return None
        return None

    async def fetchall_side(q, p=()):
        if "nodes" in q.lower():
            return []
        if "projects" in q.lower():
            return []
        return []

    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone_side)
    db.fetchall = AsyncMock(side_effect=fetchall_side)
    db.execute = AsyncMock(return_value=1)

    gw = ValidationGateway(db)
    result = asyncio.get_event_loop().run_until_complete(
        gw.check_c8_constitution_activation("cv_v1", "cv_v2")
    )
    # 충돌 없음 → 즉시 전환 결과 반환
    assert "immediately_applied" in result
    assert "suspended" in result


# ── C9: 수동 재실행 ───────────────────────────────────────────────────────

def test_c9_manual_retry_success():
    from engine.core.validation_gateway import ValidationGateway

    async def fetchone_side(q, p=()):
        return {"state": "INVALID", "version": 2}

    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone_side)
    db.execute = AsyncMock(return_value=1)

    gw = ValidationGateway(db)
    result = asyncio.get_event_loop().run_until_complete(
        gw.c9_manual_retry("node-1", "SENIOR_DESIGNER")
    )
    assert result is True


def test_c9_rejects_non_retryable_state():
    from engine.core.validation_gateway import ValidationGateway, ValidationError

    async def fetchone_side(q, p=()):
        return {"state": "COMPLETED", "version": 1}

    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone_side)
    db.execute = AsyncMock(return_value=0)

    gw = ValidationGateway(db)
    with pytest.raises(ValidationError):
        asyncio.get_event_loop().run_until_complete(
            gw.c9_manual_retry("node-1", "SENIOR_DESIGNER")
        )
