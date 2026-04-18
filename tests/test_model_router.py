"""model_router 단위 테스트 — S3-3."""
from __future__ import annotations

from engine.ai.model_adapter import ModelID
from engine.ai.model_router import estimate_cost_tier, select_model


def test_spec_preference_우선():
    m, r = select_model({"model_preference": "opus"})
    assert m == ModelID.OPUS
    assert r["rule"] == "spec_preference"


def test_auto_는_규칙_적용():
    m, r = select_model({"model_preference": "auto"}, node_type="qa")
    assert m == ModelID.HAIKU
    assert r["rule"] == "type_default"


def test_재시도_2_이상_opus_승격():
    m, r = select_model(spec=None, retry_count=2)
    assert m == ModelID.OPUS
    assert r["rule"] == "retry_escalation"


def test_truncation_이력_opus():
    m, r = select_model(
        spec=None, retry_count=0,
        failure_reasons=["max_tokens 절단 반복"],
    )
    assert m == ModelID.OPUS
    assert r["rule"] == "truncation_history"


def test_phase_bias():
    m, r = select_model(spec=None, phase="DESIGN")
    assert m == ModelID.OPUS
    assert r["rule"] == "phase_bias"


def test_fallback_sonnet():
    m, r = select_model(spec=None)
    assert m == ModelID.SONNET
    assert r["rule"] == "fallback_sonnet"


def test_cost_tier():
    assert estimate_cost_tier(ModelID.HAIKU) == 1
    assert estimate_cost_tier(ModelID.SONNET) == 3
    assert estimate_cost_tier(ModelID.OPUS) == 5


def test_short_pref_string():
    assert select_model({"model_preference": "haiku"})[0] == ModelID.HAIKU
    assert select_model({"model_preference": "sonnet"})[0] == ModelID.SONNET
