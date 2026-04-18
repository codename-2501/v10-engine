"""i18n / Locale support (S3-6).

engagement.locale 변수 분기로 한국어/영어/혼용 모두 지원. 코어 무수정 —
spec.prompt 조립 시 locale 컨텍스트만 추가, 산출물은 LLM 이 자연스럽게
선택 언어로 작성.

사용:
    from engine.i18n import get_locale, locale_directive

    locale = await get_locale(db, engagement_id)  # 'ko' | 'en' | 'ko-en'
    prompt += locale_directive(locale)
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config.thresholds import (
    DEFAULT_LOCALE,
    LOCALE_PLACEHOLDER_REPLACEMENTS,
    SUPPORTED_LOCALES,
)

logger = logging.getLogger(__name__)


async def get_locale(db: Any, engagement_id: str) -> str:
    """engagement.locale 컬럼이 있으면 사용, 없으면 default. ALTER 자동 시도."""
    if db is None:
        return DEFAULT_LOCALE
    try:
        row = await db.fetchone(
            "SELECT locale FROM engagements WHERE id=?", (engagement_id,),
        )
        if row and row.get("locale") in SUPPORTED_LOCALES:
            return row["locale"]
    except Exception:
        # 컬럼 없음 — 1회 ALTER 후 다음부터 기본값 반환
        try:
            await db.execute(
                "ALTER TABLE engagements ADD COLUMN locale TEXT DEFAULT 'ko'"
            )
        except Exception:
            pass
    return DEFAULT_LOCALE


def locale_directive(locale: str) -> str:
    """spec prompt 말미에 append 하는 locale 지시문."""
    if locale == "en":
        return (
            "\n\n## Language Directive\n"
            "Write the entire deliverable in **English**. Section headings,"
            " tables, and prose all in English. Use international/SI units"
            " where applicable.\n"
        )
    if locale == "ko-en":
        return (
            "\n\n## 언어 지침\n"
            "본문은 한국어로 작성하되, 기술 용어·고유명사·코드는 원문(영어) "
            "유지. 헤딩은 한국어 (영어 병기 OK).\n"
        )
    # ko (default)
    return (
        "\n\n## 언어 지침\n"
        "산출물 전체를 **한국어**로 작성하세요. 기술 용어는 첫 등장 시 영어 "
        "병기 가능.\n"
    )


def get_placeholder_replacement(locale: str, keyword: str) -> str:
    """auto_fix 가 locale 별 대체어를 가져갈 때 사용."""
    return (
        LOCALE_PLACEHOLDER_REPLACEMENTS.get(locale, {}).get(keyword)
        or LOCALE_PLACEHOLDER_REPLACEMENTS[DEFAULT_LOCALE].get(keyword, keyword)
    )
