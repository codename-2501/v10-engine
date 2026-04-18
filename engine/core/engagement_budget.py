"""Engagement-level token budget (S2-2).

전체 engagement 의 누적 토큰 사용량 모니터링. budget_enforcer.py(노드 단위)
는 코어 → 수정 금지. 별도 레이어로 engagement 합계 + 임계 이벤트.

기능:
- get_engagement_usage(db, engagement_id) → 누적 input/output/total
- check_budget(db, engagement_id, limit) → (within, ratio, summary)
- 80% 도달 시 warning, 100% 도달 시 PAUSE 신호 (대시보드/operator)

설계:
- agent_token_usage 테이블에 이미 노드별 기록 — engagement 로 GROUP BY 만 추가
- 코어 무수정 — watchdog/dashboard/노드 시작 hook 등에서 import
- 임계 초과 시에도 강제 stop 대신 신호만 (operator 결정)

스키마 (engagements 에 새 컬럼은 만들지 않음. 동적 limit 은 settings 테이블
또는 환경변수 사용):
    DEFAULT_ENGAGEMENT_BUDGET = 2_000_000   # tokens
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# 기본 상한 (env 로 override 가능).
import os
DEFAULT_ENGAGEMENT_BUDGET = int(os.getenv("V8_ENGAGEMENT_TOKEN_BUDGET", "2000000"))

# 경고 임계 (80% / 100%)
WARN_RATIO = 0.8
PAUSE_RATIO = 1.0


@dataclass
class BudgetSnapshot:
    engagement_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    limit: int
    ratio: float
    state: str  # 'ok' | 'warn' | 'exceed'
    summary: str


async def get_engagement_usage(
    db: Any, engagement_id: str,
) -> tuple[int, int]:
    """engagement 의 누적 input/output 토큰. 실패 시 (0, 0)."""
    if db is None:
        return 0, 0
    try:
        row = await db.fetchone(
            """SELECT
                COALESCE(SUM(input_tokens), 0)  AS input_sum,
                COALESCE(SUM(output_tokens), 0) AS output_sum
            FROM agent_token_usage atu
            JOIN nodes n ON atu.node_id = n.id
            WHERE n.engagement_id = ?""",
            (engagement_id,),
        )
        if not row:
            return 0, 0
        return int(row["input_sum"] or 0), int(row["output_sum"] or 0)
    except Exception as e:
        logger.warning("get_engagement_usage failed: %s", e)
        return 0, 0


async def check_budget(
    db: Any, engagement_id: str, limit: int = DEFAULT_ENGAGEMENT_BUDGET,
) -> BudgetSnapshot:
    """현재 사용량 vs 상한. 상태 enum 으로 분류."""
    in_t, out_t = await get_engagement_usage(db, engagement_id)
    total = in_t + out_t
    ratio = total / limit if limit > 0 else 0.0

    if ratio >= PAUSE_RATIO:
        state = "exceed"
        summary = f"engagement 토큰 {total:,}/{limit:,} ({ratio:.0%}) — 상한 초과"
    elif ratio >= WARN_RATIO:
        state = "warn"
        summary = f"engagement 토큰 {total:,}/{limit:,} ({ratio:.0%}) — 경고"
    else:
        state = "ok"
        summary = f"engagement 토큰 {total:,}/{limit:,} ({ratio:.0%})"

    return BudgetSnapshot(
        engagement_id=engagement_id,
        input_tokens=in_t,
        output_tokens=out_t,
        total_tokens=total,
        limit=limit,
        ratio=ratio,
        state=state,
        summary=summary,
    )


async def enforce_budget(
    db: Any, engagement_id: str, limit: int = DEFAULT_ENGAGEMENT_BUDGET,
) -> BudgetSnapshot:
    """check + warn/exceed 시 logger 알림 + 이벤트 카운트.

    실제 PAUSE 는 caller 책임 (이 함수는 신호만).
    """
    snap = await check_budget(db, engagement_id, limit)
    if snap.state == "warn":
        logger.warning("engagement_budget_warn engagement=%s %s",
                       engagement_id[:8], snap.summary)
    elif snap.state == "exceed":
        logger.error("engagement_budget_exceed engagement=%s %s",
                     engagement_id[:8], snap.summary)
    # observability 에 이벤트 기록 (best-effort)
    try:
        from engine.observability.events import log_event
        if snap.state in ("warn", "exceed"):
            await log_event(db, f"engagement_budget_{snap.state}",
                            project_id=engagement_id,
                            payload={"total": snap.total_tokens, "ratio": snap.ratio})
    except Exception:
        pass
    return snap
