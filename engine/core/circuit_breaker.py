"""
engine/core/circuit_breaker.py
Circuit Breaker — DB 기반 상태 (멀티 프로세스 공유).
BEGIN IMMEDIATE 트랜잭션 사용 (SQLite SELECT FOR UPDATE 미지원).
낙관적 잠금(version)으로 동시 업데이트 충돌 감지.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from engine.db.adapter import DatabaseAdapter, ConcurrentUpdateError

logger = logging.getLogger(__name__)


class CBState(str, Enum):
    CLOSED    = "CLOSED"     # 정상 — API 호출 허용
    OPEN      = "OPEN"       # 차단 — API 호출 금지
    HALF_OPEN = "HALF_OPEN"  # 복구 시도 — 제한적 허용


class CircuitBreakerOpenError(Exception):
    """CB가 OPEN 상태일 때 API 호출 시도."""
    def __init__(self, provider: str) -> None:
        super().__init__(f"CircuitBreaker OPEN: {provider} — API 호출 차단")
        self.provider = provider


class CircuitBreaker:
    """
    DB 기반 Circuit Breaker.
    상태: CLOSED → OPEN → HALF_OPEN → CLOSED
    원자성: BEGIN IMMEDIATE (DB 레벨 잠금)
    """

    FAILURE_THRESHOLD  = 5     # 연속 실패 N회 → OPEN
    RECOVERY_TIMEOUT   = 60.0  # 초: OPEN 지속 시간 → HALF_OPEN 전환
    HALF_OPEN_MAX_CALLS = 3    # HALF_OPEN에서 성공 N회 → CLOSED 복구

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    async def check(self, provider: str) -> None:
        """
        호출 전 상태 확인.
        OPEN → 자동 HALF_OPEN 전환 시도 후 판단.
        차단 상태이면 CircuitBreakerOpenError 발생.
        """
        state = await self._get_state(provider)

        if state == CBState.CLOSED or state == CBState.HALF_OPEN:
            return  # 허용

        # OPEN → recovery_timeout 경과 시 HALF_OPEN 전환
        if state == CBState.OPEN:
            await self._try_half_open(provider)
            state = await self._get_state(provider)
            if state == CBState.OPEN:
                raise CircuitBreakerOpenError(provider)

    async def record_success(self, provider: str) -> None:
        """성공 기록. HALF_OPEN에서 연속 성공 시 CLOSED 복구."""
        async with self._db.begin_immediate():
            row = await self._db.fetchone(
                "SELECT state, success_count, version FROM provider_circuit_breakers WHERE provider_name=?",
                (provider,),
            )
            if not row:
                return

            if row["state"] == CBState.HALF_OPEN:
                new_count = row["success_count"] + 1
                if new_count >= self.HALF_OPEN_MAX_CALLS:
                    await self._db.execute(
                        """UPDATE provider_circuit_breakers
                           SET state='CLOSED', failure_count=0, success_count=0,
                               opened_at=NULL, version=version+1
                           WHERE provider_name=? AND version=?""",
                        (provider, row["version"]),
                    )
                    logger.info("circuit_breaker_closed provider=%s", provider)
                else:
                    await self._db.execute(
                        """UPDATE provider_circuit_breakers
                           SET success_count=?, version=version+1
                           WHERE provider_name=? AND version=?""",
                        (new_count, provider, row["version"]),
                    )
            elif row["state"] == CBState.CLOSED:
                # 정상 상태에서 성공 → 카운터 리셋
                await self._db.execute(
                    """UPDATE provider_circuit_breakers
                       SET failure_count=0, success_count=0, version=version+1
                       WHERE provider_name=? AND version=?""",
                    (provider, row["version"]),
                )

    async def record_failure(self, provider: str) -> CBState:
        """
        실패 기록. FAILURE_THRESHOLD 도달 시 OPEN 전환.
        반환: 현재 상태.
        """
        async with self._db.begin_immediate():
            row = await self._db.fetchone(
                """SELECT failure_count, state, opened_at, version
                   FROM provider_circuit_breakers WHERE provider_name=?""",
                (provider,),
            )
            if not row:
                logger.warning("circuit_breaker_not_found provider=%s", provider)
                return CBState.CLOSED

            new_count = row["failure_count"] + 1
            new_state = row["state"]
            opened_at = row["opened_at"]

            if new_count >= self.FAILURE_THRESHOLD and row["state"] == CBState.CLOSED:
                new_state = CBState.OPEN
                opened_at = _now()
                logger.warning(
                    "circuit_breaker_opened provider=%s failure_count=%s",
                    provider,
                    new_count,
                )

            affected = await self._db.execute(
                """UPDATE provider_circuit_breakers
                   SET failure_count=?, state=?, opened_at=?,
                       last_attempt_at=?,
                       success_count=0,
                       version=version+1
                   WHERE provider_name=? AND version=?""",
                (new_count, new_state, opened_at, _now(), provider, row["version"]),
            )
            if affected == 0:
                raise ConcurrentUpdateError(
                    f"CircuitBreaker {provider}: version 충돌 — 동시 업데이트"
                )

        return CBState(new_state)

    async def get_all_states(self) -> dict[str, CBState]:
        """모든 provider의 CB 상태 조회."""
        rows = await self._db.fetchall(
            "SELECT provider_name, state FROM provider_circuit_breakers"
        )
        return {r["provider_name"]: CBState(r["state"]) for r in rows}

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    async def _get_state(self, provider: str) -> CBState:
        row = await self._db.fetchone(
            "SELECT state FROM provider_circuit_breakers WHERE provider_name=?",
            (provider,),
        )
        if not row:
            return CBState.CLOSED
        return CBState(row["state"])

    async def _try_half_open(self, provider: str) -> None:
        """OPEN 상태에서 recovery_timeout 경과 시 HALF_OPEN으로 전환."""
        row = await self._db.fetchone(
            "SELECT opened_at, state, version FROM provider_circuit_breakers WHERE provider_name=?",
            (provider,),
        )
        if not row or row["state"] != CBState.OPEN or not row["opened_at"]:
            return

        opened_at = datetime.fromisoformat(row["opened_at"])
        elapsed = (datetime.now(timezone.utc) - opened_at).total_seconds()
        if elapsed < self.RECOVERY_TIMEOUT:
            return

        async with self._db.begin_immediate():
            await self._db.execute(
                """UPDATE provider_circuit_breakers
                   SET state='HALF_OPEN', success_count=0, version=version+1
                   WHERE provider_name=? AND state='OPEN'""",
                (provider,),
            )
        logger.info("circuit_breaker_half_open provider=%s elapsed=%s", provider, elapsed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
