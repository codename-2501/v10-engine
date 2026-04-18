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
