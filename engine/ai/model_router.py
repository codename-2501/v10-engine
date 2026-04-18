"""Dynamic model router (S3-3).

노드 타입·복잡도·재시도 횟수 기반으로 Haiku/Sonnet/Opus 자동 선택.
spec 의 model_preference 가 있으면 우선 (override 금지하지 않음).

설계:
- 코어 무수정 — executor 가 호출 직전 import 해서 사용
- spec.model_preference 우선 → 없거나 'auto' 면 router 동작
- 결정 근거를 dict 로 반환 → 로깅·디버그 가능
- fallback: 결정 실패 시 SONNET (안전한 중간값)
"""

from __future__ import annotations

import logging
from typing import Any

from engine.ai.model_adapter import ModelID

logger = logging.getLogger(__name__)


# spec.type / phase 에 따른 기본 prefer
_TYPE_PREFER: dict[str, str] = {
    # QA/검증류: 짧고 빠른 판정 → Haiku
    "qa": ModelID.HAIKU,
    "verification": ModelID.HAIKU,
    "json": ModelID.HAIKU,
    # 일반 문서: Sonnet
    "document": ModelID.SONNET,
    "spec": ModelID.SONNET,
    # 코드/설계 복잡: Opus
    "design": ModelID.OPUS,
    "programmatic": ModelID.SONNET,
    "ai_code": ModelID.SONNET,
}

_PHASE_BIAS: dict[str, str] = {
    "DEFINE": ModelID.SONNET,
    "DESIGN": ModelID.OPUS,
    "BUILD": ModelID.SONNET,
    "VERIFY": ModelID.HAIKU,
    "DELIVER": ModelID.HAIKU,
}


def select_model(
    spec: dict | None = None,
    *,
    node_type: str | None = None,
    phase: str | None = None,
    retry_count: int = 0,
    failure_reasons: list | None = None,
) -> tuple[str, dict]:
    """모델 1개 선택 + 결정 근거 반환.

    Returns: (model_id, reasoning_dict)
    """
    reasoning: dict[str, Any] = {
        "spec_pref": None,
        "type": node_type,
        "phase": phase,
        "retry": retry_count,
        "rule": None,
    }

    # 1) spec 명시 우선
    if spec:
        pref = spec.get("model_preference")
        reasoning["spec_pref"] = pref
        if pref and pref != "auto":
            mapped = _resolve_pref(pref)
            if mapped:
                reasoning["rule"] = "spec_preference"
                return mapped, reasoning

    # 2) 재시도 깊을수록 강화 — 2회+ FAIL → Opus 승격
    if retry_count >= 2:
        reasoning["rule"] = "retry_escalation"
        return ModelID.OPUS, reasoning

    # 3) failure_reasons 에 'truncation' 빈발 → Opus (긴 출력 안정)
    if failure_reasons:
        text = " ".join(str(f) for f in failure_reasons[-3:]).lower()
        if "truncation" in text or "max_tokens" in text or "절단" in text:
            reasoning["rule"] = "truncation_history"
            return ModelID.OPUS, reasoning

    # 4) node_type 매핑
    if node_type and node_type in _TYPE_PREFER:
        reasoning["rule"] = "type_default"
        return _TYPE_PREFER[node_type], reasoning

    # 5) phase 기반 bias
    if phase and phase in _PHASE_BIAS:
        reasoning["rule"] = "phase_bias"
        return _PHASE_BIAS[phase], reasoning

    # 6) fallback
    reasoning["rule"] = "fallback_sonnet"
    return ModelID.SONNET, reasoning


def _resolve_pref(pref: str) -> str | None:
    """'opus' / 'sonnet' / 'haiku' 단축어 → 모델 ID."""
    p = (pref or "").strip().lower()
    if p in ("opus", "claude-opus", "claude-opus-4-6", "claude-opus-4-7"):
        return ModelID.OPUS
    if p in ("sonnet", "claude-sonnet", "claude-sonnet-4-6"):
        return ModelID.SONNET
    if p in ("haiku", "claude-haiku", "claude-haiku-4-5-20251001"):
        return ModelID.HAIKU
    # 이미 full ID 인 경우
    if p.startswith("claude-"):
        return pref
    return None


def estimate_cost_tier(model: str) -> int:
    """비용 등급 (1=저렴, 5=고가). 대시보드 요약용."""
    if "haiku" in model:
        return 1
    if "sonnet" in model:
        return 3
    if "opus" in model:
        return 5
    return 3
