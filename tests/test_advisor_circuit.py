"""Stage 5/18 Advisor CircuitBreaker 테스트 (D8 Test Suite)."""
from __future__ import annotations

import time

from engine.core.advisor import AdvisorCircuitBreaker


def test_breaker_초기_should_review_True():
    b = AdvisorCircuitBreaker(threshold=5, cooldown_s=300)
    assert b.should_review("e1", "ui") is True


def test_breaker_threshold_도달_cooldown():
    b = AdvisorCircuitBreaker(threshold=3, cooldown_s=300)
    for _ in range(3):
        b.record_reject("e1", "ui")
    # 3회 도달 → cooldown 시작
    assert b.should_review("e1", "ui") is False


def test_breaker_accept_시_streak_리셋():
    b = AdvisorCircuitBreaker(threshold=3, cooldown_s=300)
    b.record_reject("e1", "ui")
    b.record_reject("e1", "ui")
    b.record_accept("e1", "ui")  # 리셋
    b.record_reject("e1", "ui")
    # 리셋 후 1회만 → threshold 안 도달
    assert b.should_review("e1", "ui") is True


def test_breaker_다른_key_독립():
    b = AdvisorCircuitBreaker(threshold=2, cooldown_s=300)
    b.record_reject("e1", "ui")
    b.record_reject("e1", "ui")
    # ui 는 cooldown 중
    assert b.should_review("e1", "ui") is False
    # api 는 정상
    assert b.should_review("e1", "api") is True


def test_breaker_cooldown_경과_후_회복():
    b = AdvisorCircuitBreaker(threshold=2, cooldown_s=1)  # 1초
    b.record_reject("e1", "ui")
    b.record_reject("e1", "ui")
    assert b.should_review("e1", "ui") is False
    time.sleep(1.1)
    # 쿨다운 경과 → True + streak 리셋
    assert b.should_review("e1", "ui") is True


def test_snapshot_활성_breaker_표시():
    b = AdvisorCircuitBreaker(threshold=2, cooldown_s=300)
    b.record_reject("e1", "ui")
    b.record_reject("e1", "ui")
    snap = b.snapshot()
    assert len(snap["active_breakers"]) == 1
    assert snap["active_breakers"][0]["key"] == "e1:ui"
    assert snap["active_breakers"][0]["cooldown_remaining_s"] > 0
