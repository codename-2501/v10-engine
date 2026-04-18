"""
Stage 21: Failure Classifier — FAILED 노드의 원인을 분류해 자동 복구 여부 결정.

TRANSIENT: 네트워크·timeout·529/503 → 자동 재시도 (watchdog)
PERMANENT: 쿼터 소진·인증·빌링 → 즉시 NEEDS_HUMAN
UNRESOLVED: 알 수 없음 → exponential backoff N회 재시도 후 NEEDS_HUMAN

사용:
    from engine.core.failure_classifier import classify_failure, FailureClass
    cls = classify_failure(description_json_or_str)
    # cls == "transient" | "permanent" | "unresolved"
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# 자동 재시도 대상 — 일시 오류
TRANSIENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"\btimeout\b", re.I),
    re.compile(r"\bnetwork\b", re.I),
    re.compile(r"ECONNRESET", re.I),
    re.compile(r"\b(429|529|503|502|504)\b"),
    re.compile(r"temporarily unavailable", re.I),
    re.compile(r"connection reset", re.I),
    re.compile(r"connection refused", re.I),
    re.compile(r"\bDNS\b|gaierror|ENOTFOUND", re.I),
    re.compile(r"overloaded", re.I),
    re.compile(r"rate.?limit", re.I),
    re.compile(r"transient", re.I),
    re.compile(r"server busy", re.I),
    re.compile(r"capacity", re.I),
]

# 사람 개입 필수 — 재시도 무의미
PERMANENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"invalid api key", re.I),
    re.compile(r"authentication", re.I),
    re.compile(r"quota exceeded", re.I),
    re.compile(r"hit your limit", re.I),
    re.compile(r"hit_your_limit", re.I),
    re.compile(r"billing", re.I),
    re.compile(r"forbidden", re.I),
    re.compile(r"invalid_request", re.I),
    re.compile(r"invalid argument", re.I),
    re.compile(r"token_budget_exceeded|engagement_budget_exceeded", re.I),
    re.compile(r"schema validation failed.*strict", re.I),
    re.compile(r"permanent", re.I),
]


class FailureClass:
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNRESOLVED = "unresolved"


def classify_failure(description: Any) -> str:
    """description(JSON dict | str | None) 분석 후 class 반환.

    None/빈 → unresolved.
    """
    if description is None:
        return FailureClass.UNRESOLVED

    if isinstance(description, (dict, list)):
        text = json.dumps(description, ensure_ascii=False)
    else:
        text = str(description)

    text_l = text.lower()

    # 영구 오류 우선 (재시도 무의미)
    for pat in PERMANENT_PATTERNS:
        if pat.search(text_l):
            return FailureClass.PERMANENT

    # 일시 오류
    for pat in TRANSIENT_PATTERNS:
        if pat.search(text_l):
            return FailureClass.TRANSIENT

    return FailureClass.UNRESOLVED


# ---------------------------------------------------------------------------
# DB 통합 — nodes.failure_class 업데이트 (migration 027 선행 필요)
# ---------------------------------------------------------------------------

async def mark_node_failure_class(
    db: Any, node_id: str, description: Any,
) -> str:
    """노드 FAILED 전이 시 자동 classify + DB 기록.

    node_ops.mark_failed 호출 후 이 함수를 호출하면 됨 (mark_failed 자체는
    코어 기능이라 직접 수정 대신 mark_failed 래퍼에서 호출).

    반환: classification 결과 문자열.
    """
    cls = classify_failure(description)
    try:
        await db.execute(
            """UPDATE nodes
               SET failure_class=?, failure_reclassified_at=datetime('now')
               WHERE id=?""",
            (cls, node_id),
        )
    except Exception as e:
        logger.warning("failure_class_update_fail node=%s err=%s",
                       node_id[:8], str(e)[:120])
    logger.info("failure_classified node=%s class=%s", node_id[:8], cls)
    return cls


async def get_failed_nodes_by_class(
    db: Any, cls: str, limit: int = 100,
) -> list[dict]:
    """특정 class 의 FAILED 노드 목록 — watchdog 이 활용."""
    try:
        rows = await db.fetchall(
            """SELECT id, dag_id, name, retry_count, stall_count,
                      updated_at, description
               FROM nodes
               WHERE state='FAILED' AND failure_class=?
               ORDER BY updated_at ASC
               LIMIT ?""",
            (cls, limit),
        )
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.warning("get_failed_nodes_query_fail cls=%s err=%s", cls, e)
        return []
