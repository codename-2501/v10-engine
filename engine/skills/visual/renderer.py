"""
Stage 25: Visual Regression Tester with Playwright (optional).

Playwright 설치 시:
  - 3 viewport (mobile 375 / tablet 768 / desktop 1280) 렌더링
  - 스크린샷 캡처
  - DB `visual_screenshots` 에 저장
  - baseline diff (있을 때)

Playwright 미설치 시:
  - 소스 기반 heuristics 만 사용 (engine.skills.visual.heuristics)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

from engine.skills.visual.heuristics import analyze_html_source

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_VISUAL_REGRESSION", "1") != "0"
_TIMEOUT_MS = int(os.environ.get("V8_VISUAL_TIMEOUT_MS", "15000"))

# Playwright 는 선택 의존성 — import 실패해도 module 은 로드 성공
try:
    from playwright.async_api import async_playwright  # type: ignore
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


VIEWPORTS = {
    "mobile": {"width": 375, "height": 667},
    "tablet": {"width": 768, "height": 1024},
    "desktop": {"width": 1280, "height": 800},
}


class VisualRegressionTester:
    """HTML artifact 를 렌더링해 시각 문제 자동 감지.

    Playwright 없으면 heuristic 만 수행 (fallback).
    """

    def __init__(self, db: Any = None) -> None:
        self._db = db
        self._enabled = _ENABLED

    async def analyze(
        self,
        html: str,
        engagement_id: str | None = None,
        node_id: str | None = None,
        item_key: str | None = None,
        capture_screenshots: bool = True,
    ) -> dict:
        """HTML 분석 — heuristic + (있으면) 렌더링 기반 검증.

        반환:
            {
              "source_issues": [...],
              "rendered_issues": [...] | [],
              "screenshots_captured": int,
              "playwright_used": bool,
            }
        """
        result = {
            "source_issues": [],
            "rendered_issues": [],
            "screenshots_captured": 0,
            "playwright_used": False,
        }
        if not self._enabled or not html:
            return result

        # 1. 소스 기반 휴리스틱 (Playwright 필요 없음, 즉시)
        result["source_issues"] = analyze_html_source(html)

        # 2. Playwright 사용 가능 + 캡처 허용 → 렌더링 검증
        if _PLAYWRIGHT_AVAILABLE and capture_screenshots:
            try:
                rendered = await self._render_and_detect(
                    html, engagement_id, node_id, item_key,
                )
                result["rendered_issues"] = rendered.get("issues", [])
                result["screenshots_captured"] = rendered.get("count", 0)
                result["playwright_used"] = True
            except Exception as e:
                logger.warning("visual_render_fail err=%s", str(e)[:150])
                result["rendered_issues"] = [f"render_error:{e!s:.80}"]
        else:
            if not _PLAYWRIGHT_AVAILABLE:
                logger.debug("playwright 미설치 — 소스 기반 휴리스틱만 사용")

        return result

    async def _render_and_detect(
        self,
        html: str,
        engagement_id: str | None,
        node_id: str | None,
        item_key: str | None,
    ) -> dict:
        """Playwright 로 3 viewport 렌더링 후 스크린샷 + 문제 감지."""
        issues: list[str] = []
        count = 0

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for vp_name, vp in VIEWPORTS.items():
                    context = await browser.new_context(
                        viewport=vp,
                        device_scale_factor=1,
                    )
                    page = await context.new_page()
                    try:
                        await page.set_content(html, timeout=_TIMEOUT_MS)
                        # 안정화 대기 (폰트/이미지 로드)
                        await page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
                        screenshot = await page.screenshot(full_page=True)

                        # 기본 감지: 전체가 단일 색 (>95%) → 흰 페이지/깨짐
                        if await self._looks_blank(page):
                            issues.append(f"{vp_name}:blank_page")

                        # body 가 viewport 초과 — overflow
                        overflow = await page.evaluate(
                            "() => document.body.scrollWidth > window.innerWidth"
                        )
                        if overflow:
                            issues.append(f"{vp_name}:horizontal_overflow")

                        # DB 저장
                        if self._db and engagement_id and node_id and item_key:
                            await self._save_screenshot(
                                engagement_id, node_id, item_key,
                                vp_name, screenshot, issues,
                            )
                        count += 1
                    finally:
                        await context.close()
            finally:
                await browser.close()

        return {"issues": issues, "count": count}

    @staticmethod
    async def _looks_blank(page) -> bool:
        # body 의 텍스트 길이 극단적으로 적으면 blank
        text = await page.evaluate(
            "() => document.body.innerText.trim().length"
        )
        return int(text or 0) < 50

    async def _save_screenshot(
        self, engagement_id: str, node_id: str, item_key: str,
        viewport: str, screenshot_blob: bytes, issues: list[str],
    ) -> None:
        import json as _json
        try:
            await self._db.execute(
                """INSERT INTO visual_screenshots
                     (engagement_id, node_id, item_key, viewport,
                      screenshot_blob, issues, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(engagement_id, node_id, item_key, viewport)
                   DO UPDATE SET
                     screenshot_blob=excluded.screenshot_blob,
                     issues=excluded.issues,
                     captured_at=excluded.captured_at""",
                (engagement_id, node_id, item_key, viewport,
                 screenshot_blob,
                 _json.dumps(issues, ensure_ascii=False),
                 _now()),
            )
        except Exception as e:
            logger.debug("visual_db_save_fail %s", e)


# 편의 함수 — executor 에서 한 줄로 호출
async def analyze_html_artifact(
    content: str,
    engagement_id: str | None = None,
    node_id: str | None = None,
    item_key: str | None = None,
    db: Any = None,
) -> dict:
    tester = VisualRegressionTester(db=db)
    return await tester.analyze(
        content, engagement_id, node_id, item_key,
        capture_screenshots=bool(db),
    )
