"""
engine/core/budget_enforcer.py
BudgetEnforcer — L1/L2/L3 3단계 예산 집행.
TOKEN_BUDGET: MappingProxyType 불변 선언 (전역 변이 금지).
노드별 예산 오버라이드: node_budget_overrides 테이블.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TOKEN_BUDGET — 불변 선언 (v8 확정)
# 노드별 오버라이드 → node_budget_overrides 테이블에 별도 기록
# 금지: TOKEN_BUDGET['max_input'] = 200_000 → TypeError
# ---------------------------------------------------------------------------

TOKEN_BUDGET: MappingProxyType = MappingProxyType({
    "max_input":   150_000,
    "max_output":  16_000,
    "phase_limit": MappingProxyType({
        "DEFINE":          600_000,
        "DESIGN":          900_000,
        "BUILD":         1_200_000,
        "VERIFY":          500_000,
        "DELIVER":         500_000,
    }),
})


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class InputBudgetExceededError(Exception):
    """L1: Pre-call 추정 토큰이 max_input 초과."""


class PhaseBudgetExceededError(Exception):
    """L3: Phase 누적 토큰이 phase_limit 초과."""


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class BudgetSpec:
    max_input: int
    max_output: int
    phase_limit: int | None = None


@dataclass
class UsageRecord:
    input_tokens: int
    output_tokens: int
    model_name: str


# ---------------------------------------------------------------------------
# BudgetEnforcer
# ---------------------------------------------------------------------------

class BudgetEnforcer:
    """
    L1: Pre-call 추정 → 호출 전 토큰 예산 검사
    L2: max_tokens API 파라미터 → Claude API 레벨 하드 제한
    L3: Phase 누적 추적 → agent_token_usage 테이블
    """

    ESTIMATION_TOLERANCE = 1.15  # 추정치의 15% 여유 허용

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 핵심 메서드
    # ------------------------------------------------------------------

    async def pre_call_check(
        self,
        node_id: str,
        engagement_id: str,
        phase: str,
        prompt: str,
    ) -> int:
        """
        L1: Pre-call 추정 검사.
        초과 시 InputBudgetExceededError 발생.
        반환: max_output (L2 파라미터로 사용).
        """
        budget = await self.get_budget(node_id, phase)
        estimated = self._estimate_tokens(prompt)

        if estimated > budget.max_input * self.ESTIMATION_TOLERANCE:
            logger.warning(
                "budget_l1_exceeded node_id=%s estimated=%s limit=%s",
                node_id,
                estimated,
                budget.max_input,
            )
            raise InputBudgetExceededError(
                f"L1 예산 초과 — 추정 {estimated:,} > 허용 {budget.max_input:,} 토큰"
            )

        logger.debug("budget_l1_ok node_id=%s estimated=%s", node_id, estimated)
        return budget.max_output  # L2: API max_tokens 파라미터

    async def post_call_record(
        self,
        node_id: str,
        agent_run_id: str | None,
        engagement_id: str,
        phase: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        estimated_input: int | None = None,
    ) -> None:
        """
        L3: 실제 사용량 기록 + Phase 누적 검사.
        초과 시 PhaseBudgetExceededError 발생.
        """
        await self._record_usage(
            node_id, agent_run_id, engagement_id, phase,
            model_name, input_tokens, output_tokens, estimated_input,
        )

        budget = await self.get_budget(node_id, phase)
        phase_total = await self._get_phase_total(engagement_id, phase)
        phase_limit = budget.phase_limit or TOKEN_BUDGET["phase_limit"][phase]

        warn_threshold = phase_limit * 0.9
        if phase_total > warn_threshold:
            logger.warning(
                "budget_l3_warning engagement_id=%s phase=%s phase_total=%s phase_limit=%s pct=%s",
                engagement_id,
                phase,
                phase_total,
                phase_limit,
                round(phase_total / phase_limit * 100, 1),
            )

        if phase_total > phase_limit:
            raise PhaseBudgetExceededError(
                f"L3 Phase 예산 초과 — {phase}: {phase_total:,}/{phase_limit:,} 토큰"
            )

    async def get_budget(self, node_id: str, phase: str) -> BudgetSpec:
        """
        우선순위: node_budget_overrides > TOKEN_BUDGET (불변 전역).
        """
        override = await self._db.fetchone(
            "SELECT * FROM node_budget_overrides WHERE node_id=?",
            (node_id,),
        )
        if override:
            return BudgetSpec(
                max_input=override["max_input_override"] or TOKEN_BUDGET["max_input"],
                max_output=override["max_output_override"] or TOKEN_BUDGET["max_output"],
                phase_limit=override["phase_limit_override"],
            )
        return BudgetSpec(
            max_input=TOKEN_BUDGET["max_input"],
            max_output=TOKEN_BUDGET["max_output"],
        )

    # ------------------------------------------------------------------
    # 토큰 추정 (L1)
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """
        CJK 문자: 1자 ≈ 1.7 토큰.
        ASCII/기타: 1자 ≈ 0.25 토큰.
        """
        cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        ascii_count = len(text) - cjk_count
        return int(cjk_count * 1.7 + ascii_count * 0.25)

    # ------------------------------------------------------------------
    # DB 헬퍼
    # ------------------------------------------------------------------

    async def _record_usage(
        self,
        node_id: str,
        agent_run_id: str | None,
        engagement_id: str,
        phase: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        estimated_input: int | None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO agent_token_usage
               (node_id, agent_run_id, engagement_id, phase, model_name,
                input_tokens, output_tokens, estimated_input, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id, agent_run_id, engagement_id, phase, model_name,
                input_tokens, output_tokens, estimated_input,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def _get_phase_total(self, engagement_id: str, phase: str) -> int:
        row = await self._db.fetchone(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total
               FROM agent_token_usage
               WHERE engagement_id=? AND phase=?""",
            (engagement_id, phase),
        )
        return int(row["total"]) if row else 0
