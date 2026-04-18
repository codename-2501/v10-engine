"""
tests/test_state_machine.py
Hypothesis property-based 상태 전이 테스트.

실행: pytest tests/test_state_machine.py -v
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from engine.core.state_machine import (
    VALID_TRANSITIONS,
    NodeState,
    StateMachine,
)

ALL_STATES = list(VALID_TRANSITIONS.keys())


# ---------------------------------------------------------------------------
# Hypothesis: 모든 전이 경로 완전성 검증
# ---------------------------------------------------------------------------

@given(
    from_state=st.sampled_from(ALL_STATES),
    to_state=st.sampled_from(ALL_STATES),
)
@settings(max_examples=500)
def test_state_transitions_are_exhaustive(from_state: str, to_state: str) -> None:
    """VALID_TRANSITIONS 허용 경로 → 성공, 금지 경로 → InvalidTransitionError."""
    from engine.core.state_machine import InvalidTransitionError

    if to_state in VALID_TRANSITIONS[from_state]:
        result = StateMachine.transition(from_state, to_state, "test-node")
        assert result == to_state
    else:
        with pytest.raises(InvalidTransitionError):
            StateMachine.transition(from_state, to_state, "test-node")


@given(st.lists(st.sampled_from(ALL_STATES), min_size=2, max_size=10))
def test_state_transition_chain_consistency(state_chain: list[str]) -> None:
    """임의 상태 시퀀스에서 일관성 유지 — 금지 전이는 조용히 스킵."""
    current = state_chain[0]
    for target in state_chain[1:]:
        if target in VALID_TRANSITIONS.get(current, frozenset()):
            prev = current
            result = StateMachine.transition(current, target, "test-node")
            assert result == target, f"{prev} → {target} 전이 실패"
            current = result


# ---------------------------------------------------------------------------
# 단위 테스트: VALID_TRANSITIONS 구조 불변성
# ---------------------------------------------------------------------------

def test_all_states_in_valid_transitions() -> None:
    """NodeState 열거값이 모두 VALID_TRANSITIONS에 존재해야 한다."""
    for state in NodeState:
        assert state.value in VALID_TRANSITIONS, f"{state.value} 누락"


def test_valid_transitions_targets_are_valid_states() -> None:
    """전이 목표 상태도 모두 유효한 NodeState여야 한다."""
    valid = {s.value for s in NodeState}
    for from_state, targets in VALID_TRANSITIONS.items():
        for t in targets:
            assert t in valid, f"알 수 없는 목표 상태: {t}"


def test_no_self_transitions() -> None:
    """자기 자신으로의 전이는 허용하지 않는다."""
    for state, targets in VALID_TRANSITIONS.items():
        assert state not in targets, f"{state} → {state} 자기 전이 허용됨"


def test_terminal_states_have_limited_transitions() -> None:
    """FAILED 상태는 READY/INVALID로만 전이 가능."""
    assert VALID_TRANSITIONS[NodeState.FAILED.value] == frozenset({"READY", "INVALID"})


def test_is_valid() -> None:
    assert StateMachine.is_valid("READY", "IN_PROGRESS")
    assert not StateMachine.is_valid("COMPLETED", "IN_PROGRESS")
    assert StateMachine.is_valid("IN_PROGRESS", "COMPLETED")


def test_is_terminal() -> None:
    assert StateMachine.is_terminal("FAILED")
    # COMPLETED는 INVALID 전이 가능 → 터미널 아님
    assert not StateMachine.is_terminal("COMPLETED")
    assert not StateMachine.is_terminal("READY")


def test_is_active() -> None:
    assert StateMachine.is_active("IN_PROGRESS")
    assert not StateMachine.is_active("NOT_STARTED")
    assert not StateMachine.is_active("COMPLETED")


def test_is_blocking() -> None:
    assert StateMachine.is_blocking("BLOCKED")
    assert StateMachine.is_blocking("AWAITING_APPROVAL")
    # READY, IN_PROGRESS도 blocking (의존 노드는 선행 노드 완료 전까지 BLOCKED)
    assert StateMachine.is_blocking("READY")
    assert StateMachine.is_blocking("IN_PROGRESS")
    assert not StateMachine.is_blocking("COMPLETED")


# ---------------------------------------------------------------------------
# 단위 테스트: ValidationGateway C1 (DFS 순환)
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, MagicMock

from engine.core.validation_gateway import ValidationGateway, CycleError


def _make_db(edges: list[dict]) -> MagicMock:
    db = MagicMock()
    db.fetchall = AsyncMock(return_value=edges)
    db.fetchone = AsyncMock(return_value=None)
    return db


def test_c1_no_cycle_clean() -> None:
    """순환 없는 DAG → CycleError 발생 안 함."""
    edges = [
        {"from_node_id": "A", "to_node_id": "B"},
        {"from_node_id": "B", "to_node_id": "C"},
    ]
    gw = ValidationGateway(_make_db(edges))
    asyncio.get_event_loop().run_until_complete(gw.check_c1_no_cycle("dag-1"))


def test_c1_detects_cycle() -> None:
    """순환 있는 DAG → CycleError 발생."""
    edges = [
        {"from_node_id": "A", "to_node_id": "B"},
        {"from_node_id": "B", "to_node_id": "C"},
        {"from_node_id": "C", "to_node_id": "A"},  # 순환
    ]
    gw = ValidationGateway(_make_db(edges))
    with pytest.raises(CycleError):
        asyncio.get_event_loop().run_until_complete(gw.check_c1_no_cycle("dag-1"))


def test_c1_empty_dag() -> None:
    """빈 DAG → 오류 없음."""
    gw = ValidationGateway(_make_db([]))
    asyncio.get_event_loop().run_until_complete(gw.check_c1_no_cycle("dag-1"))
