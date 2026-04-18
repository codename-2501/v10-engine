"""Stage 21 failure_classifier 테스트."""
from __future__ import annotations

from engine.core.failure_classifier import classify_failure, FailureClass


def test_transient_529():
    assert classify_failure("API 오류 [529]: overloaded") == FailureClass.TRANSIENT


def test_transient_timeout():
    assert classify_failure({"error": "request timeout"}) == FailureClass.TRANSIENT


def test_transient_connection_reset():
    assert classify_failure("Connection reset by peer") == FailureClass.TRANSIENT


def test_permanent_quota():
    assert classify_failure("hit your limit") == FailureClass.PERMANENT


def test_permanent_budget():
    assert classify_failure({"msg": "token_budget_exceeded"}) == FailureClass.PERMANENT


def test_permanent_auth():
    assert classify_failure("invalid api key") == FailureClass.PERMANENT


def test_permanent_wins_over_transient():
    """both transient + permanent patterns present → permanent takes priority."""
    assert classify_failure("529 overloaded but also quota exceeded") == FailureClass.PERMANENT


def test_unresolved_unknown():
    assert classify_failure("뭔가 이상한 오류") == FailureClass.UNRESOLVED


def test_unresolved_none():
    assert classify_failure(None) == FailureClass.UNRESOLVED


def test_unresolved_empty_dict():
    assert classify_failure({}) == FailureClass.UNRESOLVED
