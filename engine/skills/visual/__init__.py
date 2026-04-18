"""
Stage 25: Visual Regression Testing.

Playwright (optional) 로 HTML 렌더링 + 스크린샷 → 휴리스틱 기반 문제 자동 감지.
OAuth 쿼터 소모 0 (CPU 만 사용).

사용 패턴 (executor.py artifact 저장 후 async):

    from engine.skills.visual import analyze_html_artifact
    issues = await analyze_html_artifact(content, engagement_id, node_id, item_key)
    # issues = ["mobile_overflow", "z_index_overlap", ...]
"""
from __future__ import annotations

from engine.skills.visual.heuristics import (
    analyze_html_source,   # Playwright 없이 소스 기반 휴리스틱
)
from engine.skills.visual.renderer import (
    analyze_html_artifact,  # Playwright 있으면 렌더링 기반, 없으면 heuristic fallback
    VisualRegressionTester,
)

__all__ = [
    "analyze_html_source",
    "analyze_html_artifact",
    "VisualRegressionTester",
]
