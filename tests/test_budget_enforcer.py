"""
tests/test_budget_enforcer.py
BudgetEnforcer L1/L2/L3 단위 테스트.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from engine.core.budget_enforcer import (
    TOKEN_BUDGET,
    BudgetEnforcer,
    InputBudgetExceededError,
    PhaseBudgetExceededError,
)


def _make_db(phase_used: int = 0) -> MagicMock:
    db = MagicMock()

    async def fetchone(query, params=()):
        q = query.lower()
        if "node_budget_overrides" in q:
            return None  # 오버라이드 없음
        if "agent_token_usage" in q or "sum(" in q:
            return {"total": phase_used}
        return None

    db.fetchone = AsyncMock(side_effect=fetchone)
    db.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=1)
    return db


# ---------------------------------------------------------------------------
# TOKEN_BUDGET 불변성
# ---------------------------------------------------------------------------

def test_token_budget_is_immutable() -> None:
    """TOKEN_BUDGET은 MappingProxyType으로 직접 수정 불가."""
    import types
    assert isinstance(TOKEN_BUDGET, types.MappingProxyType)
    with pytest.raises(TypeError):
        TOKEN_BUDGET["opus"] = 99999  # type: ignore[index]


def test_token_budget_has_expected_keys() -> None:
    assert "max_input" in TOKEN_BUDGET
    assert "max_output" in TOKEN_BUDGET
    assert "phase_limit" in TOKEN_BUDGET
    assert TOKEN_BUDGET["max_input"] > TOKEN_BUDGET["max_output"]


# ---------------------------------------------------------------------------
# L1: Pre-call 추정 검증
# ---------------------------------------------------------------------------

import asyncio


def test_l1_under_budget() -> None:
    """입력 토큰이 예산 이하 → 통과."""
    db = _make_db()
    enforcer = BudgetEnforcer(db)
    # 짧은 입력
    asyncio.get_event_loop().run_until_complete(
        enforcer.pre_call_check("node-1", "engagement-1", "DEFINE", "Hello")
    )


def test_l1_over_budget() -> None:
    """입력 토큰이 예산 초과 → InputBudgetExceededError."""
    db = _make_db()
    db.fetchall = AsyncMock(return_value=[])  # node_budget_overrides 없음
    enforcer = BudgetEnforcer(db)
    # 매우 긴 입력 생성
    huge_input = "你好世界 " * 50000  # CJK(중국어) × 1.7 → 추정치 초과
    with pytest.raises(InputBudgetExceededError):
        asyncio.get_event_loop().run_until_complete(
            enforcer.pre_call_check("node-1", "engagement-1", "DEFINE", huge_input)
        )


# ---------------------------------------------------------------------------
# L3: Phase 누적 예산 초과 검증
# ---------------------------------------------------------------------------

def test_l3_phase_within_budget() -> None:
    """Phase 누적 토큰이 한도 이하 → 통과."""
    db = _make_db(phase_used=1000)
    enforcer = BudgetEnforcer(db)
    asyncio.get_event_loop().run_until_complete(
        enforcer.post_call_record("node-1", None, "engagement-1", "DEFINE", "sonnet", 100, 200)
    )


def test_l3_phase_exceeded() -> None:
    """Phase 누적 토큰이 한도 초과 → PhaseBudgetExceededError."""
    db = _make_db(phase_used=10_000_000)  # 1천만 토큰 사용됨
    enforcer = BudgetEnforcer(db)
    with pytest.raises(PhaseBudgetExceededError):
        asyncio.get_event_loop().run_until_complete(
            enforcer.post_call_record("node-1", None, "engagement-1", "DEFINE", "sonnet", 100, 200)
        )


# ---------------------------------------------------------------------------
# _estimate_tokens: CJK vs ASCII 가중치
# ---------------------------------------------------------------------------

def test_estimate_tokens_cjk_heavier() -> None:
    """CJK(한중일 통합한자) 문자는 ASCII보다 토큰 추정치가 크다."""
    db = _make_db()
    enforcer = BudgetEnforcer(db)
    # ASCII: 4자 × 0.25 = 1
    ascii_est = enforcer._estimate_tokens("abcd")
    # CJK(중국어): 4자 × 1.7 = 6
    cjk_est = enforcer._estimate_tokens("你好世界")
    assert cjk_est > ascii_est


def test_estimate_tokens_empty() -> None:
    db = _make_db()
    enforcer = BudgetEnforcer(db)
    assert enforcer._estimate_tokens("") == 0
