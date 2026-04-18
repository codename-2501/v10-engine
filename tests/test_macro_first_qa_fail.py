"""
tests/test_macro_first_qa_fail.py  (V10)

QA FAIL 시 거시(상위) → 미시(현재) 진단 우선 처리 단위 테스트.
- 키워드 strict 매칭
- 키워드 한·영 양쪽
- AI fallback (mock)
- 카테고리 0건 → 미시 retry fallback
- 한도 초과 가드
- 모드 가드 (dry-run / keyword-only / full)
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.skills.executor_cascade import (
    _classify_upstream_categories,
    _classify_upstream_categories_ai,
    _kw_match,
    _rework_mode,
    AI_CONFIDENCE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Strict 매칭 단위 테스트
# ---------------------------------------------------------------------------

def test_kw_match_english_word_boundary():
    """영어 키워드는 word boundary 적용 — substring false positive 차단."""
    assert _kw_match("the api is broken", "api") is True
    # 'api' 가 'rapid', 'apivendor' 안에 substring 으로 있어도 매칭 안 함
    assert _kw_match("rapid changes broken", "api") is False
    assert _kw_match("apivendor system fault", "api") is False


def test_kw_match_korean_boundary():
    """한국어 키워드는 뒤에 한글 자모 조사 가능, 영문/숫자 인접 시 차단."""
    assert _kw_match("디자인 토큰이 부정확함", "디자인 토큰") is True
    assert _kw_match("디자인 토큰의 색상", "디자인 토큰") is True
    # 영문 인접은 별도 단어로 간주 → false positive 차단
    assert _kw_match("디자인 토큰abc 모름", "디자인 토큰") is False


def test_kw_match_case_insensitive():
    assert _kw_match("REST API broken", "rest api") is True
    assert _kw_match("GraphQL endpoint", "graphql") is True


# ---------------------------------------------------------------------------
# 카테고리 분류 (키워드)
# ---------------------------------------------------------------------------

def test_classify_design_korean():
    text = "디자인 토큰이 정의되지 않아 컴포넌트 색상이 누락됨"
    cats = _classify_upstream_categories(text)
    assert "DESIGN" in cats


def test_classify_api_english():
    text = "the rest api endpoint returns invalid response schema"
    cats = _classify_upstream_categories(text)
    assert "API" in cats


def test_classify_multi_categories():
    text = "디자인 시스템 누락 + api 명세 미정의로 BUILD 불가"
    cats = _classify_upstream_categories(text)
    assert "DESIGN" in cats
    assert "API" in cats


def test_classify_no_keywords_empty():
    """키워드 미매칭 시 빈 set 반환 — 미시 retry 흐름으로 fallback 보장."""
    text = "monitoring 카테고리 컴포넌트 누락"
    cats = _classify_upstream_categories(text)
    # 'monitoring' 단독은 어떤 카테고리에도 매칭 안 됨 (의도적 — 모호한 단어)
    assert cats == set()


def test_classify_extended_categories():
    """확장 카테고리 (INFRA/MOBILE/DATA) 검증 — 범용성."""
    assert "INFRA" in _classify_upstream_categories("kubernetes deployment 실패")
    assert "MOBILE" in _classify_upstream_categories("react native 빌드 오류")
    assert "DATA" in _classify_upstream_categories("dataset pipeline 결함")


# ---------------------------------------------------------------------------
# AI fallback (confidence threshold)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_fallback_high_confidence_accepted():
    """AI 분류 confidence >= threshold 면 카테고리 채택."""
    mock_adapter = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = '{"categories": ["INFRA"], "confidence": 0.85, "reasoning": "test"}'
    mock_adapter.call = AsyncMock(return_value=mock_resp)

    cats = await _classify_upstream_categories_ai(
        "배포 설정 파일이 잘못됨", mock_adapter,
    )
    assert "INFRA" in cats


@pytest.mark.asyncio
async def test_ai_fallback_low_confidence_rejected():
    """AI confidence < threshold 면 폐기 — false positive 폭주 방지."""
    mock_adapter = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = '{"categories": ["DESIGN"], "confidence": 0.3, "reasoning": "추측"}'
    mock_adapter.call = AsyncMock(return_value=mock_resp)

    cats = await _classify_upstream_categories_ai("애매한 사유", mock_adapter)
    assert cats == set()


@pytest.mark.asyncio
async def test_ai_fallback_invalid_json_safe():
    """AI 응답이 JSON 아니면 빈 set 안전 반환."""
    mock_adapter = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = "분석 불가"
    mock_adapter.call = AsyncMock(return_value=mock_resp)

    cats = await _classify_upstream_categories_ai("test", mock_adapter)
    assert cats == set()


@pytest.mark.asyncio
async def test_ai_fallback_no_adapter_safe():
    """model_adapter=None 이면 즉시 빈 set."""
    cats = await _classify_upstream_categories_ai("test", None)
    assert cats == set()


@pytest.mark.asyncio
async def test_ai_fallback_filters_unknown_categories():
    """카테고리 화이트리스트 외 값은 필터링."""
    mock_adapter = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = (
        '{"categories": ["DESIGN", "BLOCKCHAIN", "QUANTUM"], '
        '"confidence": 0.9, "reasoning": "test"}'
    )
    mock_adapter.call = AsyncMock(return_value=mock_resp)

    cats = await _classify_upstream_categories_ai("test", mock_adapter)
    assert cats == {"DESIGN"}


# ---------------------------------------------------------------------------
# 모드 가드
# ---------------------------------------------------------------------------

def test_rework_mode_default_keyword_only(monkeypatch):
    """기본값은 keyword-only (안전한 default — AI fallback 명시적 opt-in)."""
    monkeypatch.delenv("V10_UPSTREAM_REWORK_MODE", raising=False)
    assert _rework_mode() == "keyword-only"


def test_rework_mode_dryrun(monkeypatch):
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "dry-run")
    assert _rework_mode() == "dry-run"


def test_rework_mode_keyword_only(monkeypatch):
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    assert _rework_mode() == "keyword-only"


def test_ai_confidence_threshold_value():
    """confidence threshold 가 합리적 범위 (0.5~0.9) 인지 sanity check."""
    assert 0.5 <= AI_CONFIDENCE_THRESHOLD <= 0.9


# ---------------------------------------------------------------------------
# 갭 보강 — TASK 예외 / SUSPENDED / BLOCKED 경로 검증
# ---------------------------------------------------------------------------

def test_task_exception_short_message_no_classify():
    """예외 메시지가 짧고 추상적이면 키워드 매칭 0건 (false positive 차단)."""
    # 30자 이하 추상 메시지 — 카테고리 검출 불가
    cats = _classify_upstream_categories("ValueError")
    assert cats == set()
    cats = _classify_upstream_categories("None")
    assert cats == set()


def test_task_exception_rich_message_classified():
    """예외 메시지에 카테고리 키워드 포함 시 정확히 분류."""
    text = "TimeoutError: rest api endpoint /users 응답 5s 초과"
    cats = _classify_upstream_categories(text)
    assert "API" in cats


def test_suspended_stall_text_classified():
    """SUSPENDED 직전 stall 사유에 키워드 있으면 거시 진단 가능."""
    # _reason_json 에서 추출되는 failures 텍스트 시뮬레이션
    stall_text = (
        "QA 연속 실패 2회로 무한 루프 방지를 위해 중단됨. "
        "디자인 토큰 색상 정의 누락"
    )
    cats = _classify_upstream_categories(stall_text)
    assert "DESIGN" in cats


def test_blocked_blocker_failure_classified():
    """BLOCKED 시 blocker failure_reasons 에서 키워드 추출 가능."""
    # blocker 의 last reason 시뮬레이션
    blocker_reason = (
        "QA 판정 FAIL (score=12): db 스키마 미정의로 마이그레이션 실패"
    )
    cats = _classify_upstream_categories(blocker_reason)
    assert "DB" in cats


@pytest.mark.asyncio
async def test_hook_safe_with_none_db():
    """db=None 또는 빈 텍스트 시 즉시 0 반환 (raise 없음)."""
    from engine.skills.executor_cascade import trigger_upstream_rework_if_needed
    # db=None 가드
    result = await trigger_upstream_rework_if_needed(
        None, "qa-id", "task-id", "디자인 토큰 누락",
    )
    assert result == 0
    # 빈 텍스트 가드
    result = await trigger_upstream_rework_if_needed(
        MagicMock(), "qa-id", "task-id", "",
    )
    assert result == 0
