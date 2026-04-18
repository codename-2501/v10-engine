"""Gotcha 학습 모듈 — 반복 실패 패턴 기록 및 로드.

executor.py에서 분리된 독립 모듈. DB 기록만 담당하며 side effect 최소화.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def classify_gotcha(failure_text: str) -> str:
    """실패 텍스트를 gotcha 카테고리로 분류."""
    if "403" in failure_text or "permission" in failure_text.lower():
        return "permission_denied"
    if "404" in failure_text or "not found" in failure_text.lower():
        return "not_found"
    if "timeout" in failure_text.lower() or "timed out" in failure_text.lower():
        return "timeout"
    if "validation" in failure_text.lower():
        return "validation_error"
    return "generic"


async def record_gotcha(
    db,
    engagement_id: str,
    node_id: str,
    gotcha_class: str,
    failure_detail: str,
) -> bool:
    """Gotcha를 DB에 기록."""
    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO gotchas_learned
            (engagement_id, node_id, gotcha_class, failure_detail, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(engagement_id, node_id, gotcha_class)
            DO UPDATE SET count = count + 1
            """,
            (engagement_id, node_id, gotcha_class, failure_detail),
        )
        return True
    except Exception as e:
        logger.warning("Failed to record gotcha: %s", e)
        return False


async def load_gotchas_for_prompt(
    db,
    engagement_id: str,
) -> list[dict[str, Any]]:
    """이전에 학습한 gotcha 목록을 프롬프트용으로 로드."""
    try:
        rows = await db.fetchall(
            """
            SELECT node_id, gotcha_class, failure_detail, count
            FROM gotchas_learned
            WHERE engagement_id = ?
            ORDER BY count DESC
            LIMIT 10
            """,
            (engagement_id,),
        )
        return [
            {
                "node_id": row[0],
                "class": row[1],
                "detail": row[2],
                "count": row[3],
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning("Failed to load gotchas: %s", e)
        return []
