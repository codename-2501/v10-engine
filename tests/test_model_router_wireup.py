"""model_router wire-up 회귀 테스트 — S5-C.

executor 가 QA 호출 직전 select_model() 호출하는 흐름 검증. 실제 executor 는
외부 의존(DB·LLM) 이 많아 여기서는 select_model() 의 반환값이 QA 문맥에서
기대대로 나오는지만 확인 (unit level).
"""
from __future__ import annotations

from engine.ai.model_adapter import ModelID
from engine.ai.model_router import select_model


def test_QA_기본_Haiku():
    """QA 노드 + retry 0 → Haiku (type_default)."""
    m, r = select_model(
        spec={"model_preference": "auto"},
        node_type="qa", retry_count=0,
    )
    assert m == ModelID.HAIKU
    assert r["rule"] == "type_default"


def test_QA_retry2_Opus승격():
    """retry≥2 면 Opus 로 에스컬레이션."""
    m, r = select_model(
        spec=None,
        node_type="qa", retry_count=2,
    )
    assert m == ModelID.OPUS
    assert r["rule"] == "retry_escalation"


def test_QA_spec_opus_우선():
    """spec.model_preference='opus' 는 retry_count 0 라도 우선."""
    m, r = select_model(
        spec={"model_preference": "opus"},
        node_type="qa", retry_count=0,
    )
    assert m == ModelID.OPUS
    assert r["rule"] == "spec_preference"


def test_QA_truncation_이력_Opus():
    """failure_reasons 에 truncation/max_tokens 키워드 → Opus."""
    m, r = select_model(
        spec=None, node_type="qa", retry_count=1,
        failure_reasons=["max_tokens 절단 반복", "truncation detected"],
    )
    assert m == ModelID.OPUS
    assert r["rule"] == "truncation_history"


def test_QA_phase_DESIGN_bias():
    """node_type 없으면 phase 기반 bias."""
    m, r = select_model(
        spec=None, phase="DESIGN", retry_count=0,
    )
    assert m == ModelID.OPUS
    assert r["rule"] == "phase_bias"


def test_QA_spec_none_fallback():
    """spec=None 에 node_type 도 알 수 없으면 Sonnet fallback."""
    m, r = select_model(spec=None)
    assert m == ModelID.SONNET
    assert r["rule"] == "fallback_sonnet"
