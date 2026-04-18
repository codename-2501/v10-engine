"""
engine/core/state_machine.py
노드 상태 머신 — 10-state + VALID_TRANSITIONS.
Rule 8: 모든 규칙 MANDATORY, AI 임의 해석 및 예외 금지.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class InvalidTransitionError(Exception):
    """허용되지 않은 상태 전이 시도."""

    def __init__(self, from_state: str, to_state: str, node_id: str = "") -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.node_id = node_id
        label = f" (node={node_id})" if node_id else ""
        super().__init__(
            f"InvalidTransition{label}: {from_state} → {to_state} 는 허용되지 않음"
        )


# ---------------------------------------------------------------------------
# 상태 열거값 (10가지)
# ---------------------------------------------------------------------------

class NodeState(str, Enum):
    NOT_STARTED       = "NOT_STARTED"       # 초기 상태
    READY             = "READY"             # 의존성 충족, 실행 대기
    IN_PROGRESS       = "IN_PROGRESS"       # 에이전트 실행 중
    COMPLETED         = "COMPLETED"         # TASK + QA 통과
    BLOCKED           = "BLOCKED"           # 선행 미완료
    INVALID           = "INVALID"           # 상위 변경으로 무효
    AWAITING_APPROVAL = "AWAITING_APPROVAL" # Gate/외부 승인 대기
    NEEDS_HUMAN       = "NEEDS_HUMAN"       # 3회 실패, 설계자 개입
    SUSPENDED         = "SUSPENDED"         # 예산 초과 또는 CB 차단 (v5)
    FAILED            = "FAILED"            # 복구 불가
    SKIPPED           = "SKIPPED"           # 비해당 — 실행 안 함, 하위 노드 안 막음


# ---------------------------------------------------------------------------
# MANDATORY 상태 전이 테이블 (엔진 하드코딩 — 절대 변경 불가)
# Rule 8: 예외 없음
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    NodeState.NOT_STARTED:       frozenset({NodeState.READY, NodeState.BLOCKED, NodeState.INVALID, NodeState.SKIPPED}),
    NodeState.READY:             frozenset({NodeState.IN_PROGRESS, NodeState.BLOCKED, NodeState.INVALID}),
    NodeState.IN_PROGRESS:       frozenset({
                                     NodeState.COMPLETED, NodeState.FAILED, NodeState.INVALID,
                                     NodeState.NEEDS_HUMAN, NodeState.SUSPENDED, NodeState.AWAITING_APPROVAL,
                                     NodeState.READY,  # 자동 재시도 (retry_count < max_retries)
                                 }),
    NodeState.COMPLETED:         frozenset({NodeState.INVALID}),
    NodeState.BLOCKED:           frozenset({NodeState.READY, NodeState.NOT_STARTED, NodeState.INVALID}),
    NodeState.INVALID:           frozenset({NodeState.READY, NodeState.NOT_STARTED, NodeState.BLOCKED}),
    NodeState.AWAITING_APPROVAL: frozenset({NodeState.COMPLETED, NodeState.INVALID}),
    NodeState.NEEDS_HUMAN:       frozenset({NodeState.IN_PROGRESS, NodeState.INVALID, NodeState.FAILED}),
    NodeState.SUSPENDED:         frozenset({NodeState.READY, NodeState.INVALID, NodeState.FAILED}),
    NodeState.FAILED:            frozenset({NodeState.READY, NodeState.INVALID}),
    NodeState.SKIPPED:           frozenset({NodeState.READY, NodeState.INVALID}),  # 비해당→해당으로 변경 시
}


# ---------------------------------------------------------------------------
# SUSPENDED 상태 이유 (v5 신규 — suspension_reason 컬럼)
# ---------------------------------------------------------------------------

class SuspensionReason(str, Enum):
    BUDGET_EXCEEDED  = "BUDGET_EXCEEDED"   # BudgetEnforcer L3 초과
    CIRCUIT_BREAKER  = "CIRCUIT_BREAKER"   # CircuitBreaker OPEN
    SHUTDOWN_DRAIN   = "SHUTDOWN_DRAIN"    # ShutdownManager graceful drain


# ---------------------------------------------------------------------------
# Phase 열거값
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    DEFINE  = "DEFINE"
    DESIGN  = "DESIGN"
    BUILD   = "BUILD"
    VERIFY  = "VERIFY"
    DELIVER = "DELIVER"


PHASE_ORDER: list[Phase] = [
    Phase.DEFINE,
    Phase.DESIGN,
    Phase.BUILD,
    Phase.VERIFY,
    Phase.DELIVER,
]


# ---------------------------------------------------------------------------
# NodeType
# ---------------------------------------------------------------------------

class NodeType(str, Enum):
    TASK = "TASK"
    QA   = "QA"
    GATE = "GATE"


# ---------------------------------------------------------------------------
# 상태 머신 (순수 함수형 — DB 의존 없음)
# ---------------------------------------------------------------------------

class StateMachine:
    """
    상태 전이 검증 + 적용.
    DB 저장은 호출자 책임.
    """

    @staticmethod
    def is_valid(from_state: str, to_state: str) -> bool:
        """전이 허용 여부 확인."""
        allowed = VALID_TRANSITIONS.get(from_state, frozenset())
        return to_state in allowed

    @staticmethod
    def transition(from_state: str, to_state: str, node_id: str = "") -> str:
        """
        상태 전이 실행.
        성공 시 to_state 반환.
        실패 시 InvalidTransitionError 발생.
        """
        if not StateMachine.is_valid(from_state, to_state):
            raise InvalidTransitionError(from_state, to_state, node_id)
        return to_state

    @staticmethod
    def allowed_transitions(from_state: str) -> frozenset[str]:
        """특정 상태에서 전이 가능한 상태 목록."""
        return VALID_TRANSITIONS.get(from_state, frozenset())

    @staticmethod
    def is_terminal(state: str) -> bool:
        """에이전트가 개입할 수 없는 종료 상태 여부."""
        return state in {NodeState.FAILED}

    @staticmethod
    def is_active(state: str) -> bool:
        """에이전트가 실행 중인 상태 여부."""
        return state == NodeState.IN_PROGRESS

    @staticmethod
    def is_blocking(state: str) -> bool:
        """하위 노드를 BLOCKED 상태로 만드는 상태 여부."""
        return state in {
            NodeState.NOT_STARTED,
            NodeState.READY,
            NodeState.IN_PROGRESS,
            NodeState.BLOCKED,
            NodeState.INVALID,
            NodeState.AWAITING_APPROVAL,
            NodeState.NEEDS_HUMAN,
            NodeState.SUSPENDED,
            NodeState.FAILED,
        }
