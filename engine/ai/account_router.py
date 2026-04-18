"""
engine/ai/account_router.py
멀티 CLI 계정 라우터 — 모델 기반 라우팅 + 사용량 기반 자동 전환.

규칙:
  1. Opus 모델 → max 티어 계정만 (pro는 Opus 사용 불가)
  2. Sonnet/Haiku → 사용량 적은 계정 우선 (라운드로빈 + 사용량 균형)
  3. 계정 1개면 기존과 동일 동작
  4. CLI 실패 시 다른 계정으로 자동 전환
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List

logger = logging.getLogger(__name__)


@dataclass
class AccountStats:
    """계정별 인메모리 사용량 추적."""
    name: str
    tier: str  # "max" | "pro"
    adapter: Any  # CLIProxyAdapter
    daily_tokens: int = 0
    daily_calls: int = 0
    last_reset: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    # Stage 11 강화: 529/overload 별도 추적 + 일시 cool-down
    overload_count: int = 0
    cooldown_until: float = 0.0  # epoch seconds


class AccountRouter:
    """모델 기반 + 사용량 기반 계정 자동 라우팅.

    ModelAdapter와 동일한 call() 인터페이스를 제공하므로
    executor에서 기존 adapter 대신 drop-in 교체 가능.

    Stage 11 강화:
    - 일일 토큰 사용량 임계(V8_MAX_DAILY_TOKENS_WARN, 기본 400K) 넘으면
      Pro 로 preemptive 라우팅 (Max 보전)
    - 529/overload 연속 발생 시 임계(기본 3회) 넘으면 해당 계정 300s cool-down
    - cool-down 중엔 후보에서 제외 → 다른 계정 사용
    """

    # Stage 11 튜닝 상수 (env override)
    import os as _os
    MAX_DAILY_TOKENS_WARN = int(_os.environ.get("V8_MAX_DAILY_TOKENS_WARN", "400000"))
    OVERLOAD_THRESHOLD = int(_os.environ.get("V8_OVERLOAD_COOLDOWN_THRESHOLD", "3"))
    OVERLOAD_COOLDOWN_S = int(_os.environ.get("V8_OVERLOAD_COOLDOWN_S", "300"))

    def __init__(self, accounts: List[AccountStats], db: Any = None) -> None:
        self._accounts = accounts
        self._db = db  # DB 참조 — CLI 계정 is_active 실시간 체크용
        self._lock = asyncio.Lock()
        # TTL 캐시: _get_active_names (30초)
        self._active_names_cache: set[str] | None = None
        self._active_names_ts: float = 0.0
        # 초기 로그
        names = [(a.name, a.tier) for a in accounts]
        logger.info("account_router_initialized accounts=%s", names)

    @property
    def accounts(self) -> List[AccountStats]:
        return self._accounts

    async def call(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str = "",
        temperature: float = 0.3,
    ) -> Any:
        """모델에 따라 적절한 계정을 선택하여 호출."""
        account = await self._select_account(model)
        try:
            resp = await account.adapter.call(
                model=model,
                prompt=prompt,
                max_tokens=max_tokens,
                system=system,
                temperature=temperature,
            )
            # 성공 — 사용량 기록
            async with self._lock:
                account.daily_tokens += (resp.input_tokens or 0) + (resp.output_tokens or 0)
                account.daily_calls += 1
                account.consecutive_failures = 0
                # Stage 11: 성공 시 overload 카운터도 점진 회복
                if account.overload_count > 0:
                    account.overload_count = max(0, account.overload_count - 1)
            return resp

        except Exception as exc:
            # 실패 — 다른 계정으로 재시도
            async with self._lock:
                account.consecutive_failures += 1
                # Stage 11: 529/overload 감지 시 별도 카운터 + cool-down
                err_str = str(exc).lower()
                is_overload = any(k in err_str for k in ("529", "overload", "capacity", "hit your limit"))
                if is_overload:
                    account.overload_count += 1
                    if account.overload_count >= self.OVERLOAD_THRESHOLD:
                        account.cooldown_until = time.time() + self.OVERLOAD_COOLDOWN_S
                        logger.warning(
                            "account_cooldown_tripped name=%s overloads=%d cooldown=%ds",
                            account.name, account.overload_count, self.OVERLOAD_COOLDOWN_S,
                        )

            fallback = await self._select_fallback(model, exclude=account)
            if fallback and fallback is not account:
                logger.warning(
                    "account_failover from=%s to=%s error=%s",
                    account.name, fallback.name, str(exc)[:100],
                )
                resp = await fallback.adapter.call(
                    model=model, prompt=prompt, max_tokens=max_tokens,
                    system=system, temperature=temperature,
                )
                async with self._lock:
                    fallback.daily_tokens += (resp.input_tokens or 0) + (resp.output_tokens or 0)
                    fallback.daily_calls += 1
                return resp
            else:
                raise  # 대체 계정 없음 — 원래 에러 전파

    async def _get_active_names(self) -> set[str] | None:
        """DB에서 현재 활성 CLI 계정 이름 조회. 30초 TTL 캐시 적용."""
        if not self._db:
            return None
        now = time.time()
        if self._active_names_cache is not None and (now - self._active_names_ts) < 30.0:
            return self._active_names_cache
        try:
            rows = await self._db.fetchall(
                "SELECT name FROM cli_accounts WHERE is_active=1"
            )
            self._active_names_cache = {r["name"] for r in rows}
            self._active_names_ts = now
            return self._active_names_cache
        except Exception:
            return None  # DB 오류 시 전부 허용 (안전 fallback)

    async def _select_account(self, model: str) -> AccountStats:
        """모델에 따라 최적 계정 선택."""
        active_names = await self._get_active_names()

        async with self._lock:
            self._maybe_reset_daily()

            is_opus = "opus" in model.lower()

            now_ts = time.time()
            candidates = []
            for a in self._accounts:
                if active_names is not None and a.name not in active_names:
                    continue  # DB에서 비활성화된 계정 제외
                if is_opus and a.tier != "max":
                    continue  # Opus는 max만
                if a.consecutive_failures >= 3:
                    continue  # 연속 3회 실패 계정 제외
                # Stage 11: cool-down 중 계정 제외 (overload 임계 초과)
                if a.cooldown_until > now_ts:
                    continue
                # Stage 11: Max 계정 + 일일 토큰 임계 초과 → Pro 있으면 보전 목적으로 제외
                if a.tier == "max" and a.daily_tokens >= self.MAX_DAILY_TOKENS_WARN:
                    pro_available = any(
                        p.tier == "pro"
                        and (active_names is None or p.name in active_names)
                        and p.cooldown_until <= now_ts
                        and p.consecutive_failures < 3
                        for p in self._accounts
                    )
                    if pro_available and not is_opus:
                        # Opus 아니면 Pro 로 돌려서 Max 보전
                        logger.info(
                            "account_max_preserve name=%s daily_tokens=%d (> %d) → prefer pro",
                            a.name, a.daily_tokens, self.MAX_DAILY_TOKENS_WARN,
                        )
                        continue
                candidates.append(a)

            if not candidates:
                # 연속 실패로 전부 제외된 경우만 리셋 (비활성 계정은 복구 안 함)
                active_accounts = [
                    a for a in self._accounts
                    if active_names is None or a.name in active_names
                ]
                if active_accounts:
                    # 활성 계정은 있는데 실패로 제외됨 → 실패 카운터 리셋 후 재시도
                    for a in active_accounts:
                        a.consecutive_failures = 0
                    candidates = [a for a in active_accounts if not (is_opus and a.tier != "max")]
                    if not candidates:
                        candidates = active_accounts
                else:
                    # 활성 계정이 아예 없음 → 에러
                    raise RuntimeError("활성 CLI 계정이 없습니다. 인증 관리에서 계정을 활성화하세요.")

            # 사용량 적은 계정 우선
            return min(candidates, key=lambda a: a.daily_tokens)

    async def _select_fallback(self, model: str, exclude: AccountStats) -> AccountStats | None:
        """exclude 제외하고 대체 계정 선택."""
        active_names = await self._get_active_names()
        async with self._lock:
            is_opus = "opus" in model.lower()
            for a in self._accounts:
                if a is exclude:
                    continue
                if active_names is not None and a.name not in active_names:
                    continue
                if is_opus and a.tier != "max":
                    continue
                return a
        return None

    def _maybe_reset_daily(self) -> None:
        """자정 기준 일일 카운터 리셋."""
        now = time.time()
        for a in self._accounts:
            if now - a.last_reset > 86400:  # 24시간
                a.daily_tokens = 0
                a.daily_calls = 0
                a.last_reset = now
                logger.info("account_daily_reset name=%s", a.name)
