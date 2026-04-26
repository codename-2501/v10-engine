"""Domain profile loader (S3-4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROFILES_DIR = Path(__file__).parent


def list_profiles() -> list[str]:
    """사용 가능한 프로파일 이름 목록 (yaml 파일명 stem)."""
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))


def load_profile(name: str) -> dict[str, Any] | None:
    """프로파일 1개 로드. 없으면 None."""
    path = _PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def detect_profile(project_text: str) -> str | None:
    """프로젝트 설명 텍스트에서 도메인 키워드 매칭으로 profile 추정.

    LLM 호출 없이 키워드 매치만 — 빠르고 결정적. 실패 시 None.

    score 동률 시 첫 매칭 (alphabetical) 반환 — Pillar 2 의 hybrid 가
    LLM 분류로 보정하므로 키워드 fallback 만으로 결정 안 함.
    """
    text = (project_text or "").lower()
    scores: dict[str, int] = {}
    for name in list_profiles():
        profile = load_profile(name) or {}
        for kw in profile.get("keywords", []):
            if str(kw).lower() in text:
                scores[name] = scores.get(name, 0) + 1
    if not scores:
        return None
    return max(scores.items(), key=lambda x: x[1])[0]


async def classify_domain_llm(
    project_text: str, model_adapter: Any
) -> dict[str, Any] | None:
    """LLM 기반 도메인 분류 (Pillar 2 hybrid).

    LLM 에 프로젝트 설명 + 후보 도메인 list 를 주고 best-fit + confidence 반환.
    실패 시 None — 호출자는 키워드 fallback 사용.

    Returns: {"top": str, "confidence": float, "candidates": [{"name", "score"}]}
    """
    candidates = list_profiles()
    if not candidates:
        return None
    candidate_lines = "\n".join(
        f"  - {name}: {(load_profile(name) or {}).get('name', name)}"
        for name in candidates
    )
    prompt = (
        "다음 프로젝트의 도메인을 아래 후보 중 하나로 분류해. "
        "본문 의미를 우선시하되 후보의 키워드/특성도 참고. "
        "확신 정도(0.0~1.0)도 함께 출력.\n\n"
        f"## 후보\n{candidate_lines}\n\n"
        f"## 프로젝트 설명\n{project_text[:3000]}\n\n"
        "## 출력 (순수 JSON, 코드블록 없음)\n"
        '{"top": "<후보_이름>", "confidence": 0.0~1.0, '
        '"reason": "<한 줄 근거>"}'
    )
    try:
        from engine.ai.model_router import ModelID
        response = await model_adapter.call(
            model=ModelID.HAIKU if hasattr(ModelID, "HAIKU") else "haiku",
            prompt=prompt,
            max_tokens=300,
            system="너는 SI 프로젝트 도메인 분류 전문가다.",
        )
        import json as _json
        content = getattr(response, "content", str(response)).strip()
        # 코드블록 제거
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(
                line for line in lines if not line.startswith("```")
            )
        data = _json.loads(content)
        top = data.get("top")
        if top not in candidates:
            return None
        return {
            "top": top,
            "confidence": float(data.get("confidence", 0.5)),
            "reason": data.get("reason", ""),
        }
    except Exception:
        return None


async def detect_profile_hybrid(
    project_text: str,
    model_adapter: Any | None = None,
    confidence_threshold: float = 0.85,
) -> str:
    """Pillar 2 — 키워드 + LLM hybrid 도메인 분류.

    1. 키워드 매치 (현재 detect_profile)
    2. LLM 분류 (model_adapter 있을 때만)
    3. 두 결과 일치 → 그것 채택
    4. 불일치 + LLM confidence > threshold → LLM
    5. 그 외 → 키워드 결과 또는 'general' fallback

    LLM 실패/None 시 graceful 키워드 결과로 진행.
    """
    keyword_result = detect_profile(project_text)
    if model_adapter is None:
        return keyword_result or "general"

    llm_result = await classify_domain_llm(project_text, model_adapter)
    if llm_result is None:
        return keyword_result or "general"

    llm_top = llm_result["top"]
    llm_conf = llm_result["confidence"]

    if keyword_result == llm_top:
        return keyword_result
    if llm_conf >= confidence_threshold:
        return llm_top
    return keyword_result or "general"
