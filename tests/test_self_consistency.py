"""self_consistency.py 단위 테스트 — S2-4."""
from __future__ import annotations

import pytest

from engine.skills.qa.self_consistency import (
    DEFAULT_N,
    DEFAULT_WHITELIST,
    parse_verdict,
    run_consistency_qa,
    should_apply,
)


# ---------------------------------------------------------------------------
# should_apply
# ---------------------------------------------------------------------------

def test_explicit_n_사용():
    assert should_apply({"self_consistency_n": 5}) == 5


def test_explicit_n_cap_5():
    assert should_apply({"self_consistency_n": 99}) == 5


def test_explicit_n_1_미적용():
    assert should_apply({"self_consistency_n": 1}) == 0


def test_화이트리스트_기본_적용():
    """S6.2 이후 DEFAULT_WHITELIST 비어있음. opt-in (spec.self_consistency_n)만 적용."""
    assert should_apply({"name": "PRD"}) == 0
    assert should_apply({}, task_name="화면 목록 정의서") == 0
    assert should_apply({"name": "리스크 관리 계획서"}) == 0
    # opt-in 은 여전히 동작
    assert should_apply({"self_consistency_n": 3}) == 3


def test_비대상_미적용():
    assert should_apply({"name": "그냥문서"}) == 0


def test_None_spec_미적용():
    assert should_apply(None) == 0


# ---------------------------------------------------------------------------
# parse_verdict
# ---------------------------------------------------------------------------

def test_PASS_명시():
    s, p = parse_verdict("점수: 75\n판정: PASS")
    assert s == 75 and p is True


def test_FAIL_우선():
    """PASS, FAIL 둘 다 있으면 FAIL (안전 우선)."""
    s, p = parse_verdict("FAIL 사유 — PASS 기준 미달")
    assert p is False


def test_점수_50_이상_implicit_PASS():
    s, p = parse_verdict("score=80 검토 완료")
    assert s == 80 and p is True


def test_점수_50_미만_implicit_FAIL():
    s, p = parse_verdict("score=30 검토 완료")
    assert s == 30 and p is False


def test_빈문자열():
    assert parse_verdict("") == (0, False)


# ---------------------------------------------------------------------------
# run_consistency_qa — async
# ---------------------------------------------------------------------------

class _MockResp:
    def __init__(self, content):
        self.content = content


@pytest.mark.asyncio
async def test_n3_다수결_PASS():
    """3개 중 2개 PASS → final_pass = True."""
    responses = iter([
        _MockResp("score: 70 PASS"),
        _MockResp("score: 65 PASS"),
        _MockResp("score: 30 FAIL"),
    ])

    async def call():
        return next(responses)

    v = await run_consistency_qa(3, call)
    assert v.n == 3
    assert v.final_pass is True
    assert v.median_score == 65
    assert v.pass_rate == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_n3_다수결_FAIL():
    responses = iter([
        _MockResp("score: 30 FAIL"),
        _MockResp("score: 25 FAIL"),
        _MockResp("score: 80 PASS"),
    ])

    async def call():
        return next(responses)

    v = await run_consistency_qa(3, call)
    assert v.final_pass is False
    assert v.median_score == 30


@pytest.mark.asyncio
async def test_n1_단일호출_경로():
    async def call():
        return _MockResp("score: 75 PASS")
    v = await run_consistency_qa(1, call)
    assert v.n == 1
    assert v.final_pass is True


@pytest.mark.asyncio
async def test_예외_보수_처리():
    """일부 호출 예외 → 0점/FAIL 로 합산되어 다수결 영향."""
    responses = iter([
        _MockResp("PASS score: 80"),
        Exception("API down"),
        _MockResp("PASS score: 75"),
    ])

    async def call():
        x = next(responses)
        if isinstance(x, Exception):
            raise x
        return x

    v = await run_consistency_qa(3, call)
    # 2 PASS + 1 error → 다수결 PASS
    assert v.final_pass is True
    assert v.pass_rate == pytest.approx(2 / 3)
