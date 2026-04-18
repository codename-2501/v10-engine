"""
engine/observability/alert_rules.py
AlertRules — 임계값 기반 알림.
AlertManager: asyncio.sleep(30) 루프로 주기적 체크.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from engine.observability.metrics import (
    ACTIVE_AGENTS, CB_STATE, OUTBOX_QUEUE_DEPTH
)

logger = logging.getLogger(__name__)

_ALERT_WEBHOOK = os.environ.get("V9_ALERT_WEBHOOK_URL")


async def _post_slack(message: str) -> None:
    """Slack webhook으로 알림 전송. 실패해도 조용히 넘어감."""
    if not _ALERT_WEBHOOK:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await s.post(
                _ALERT_WEBHOOK,
                json={"text": message},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        pass  # 알림 실패가 운영에 영향 없게


@dataclass
class AlertRule:
    name: str
    condition: Callable[[], bool]
    severity: str          # 'CRITICAL' | 'HIGH' | 'WARNING'
    message: str
    cooldown: int          # 초: 같은 alert 재발화 방지
    _last_fired_at: float = field(default=0.0, repr=False)

    def should_fire(self) -> bool:
        try:
            triggered = self.condition()
        except Exception:
            return False
        if not triggered:
            return False
        now = datetime.now(timezone.utc).timestamp()
        return (now - self._last_fired_at) >= self.cooldown

    def record_fired(self) -> None:
        self._last_fired_at = datetime.now(timezone.utc).timestamp()


def _safe_gauge_value(metric, label_values: dict | None = None) -> float:
    """prometheus_client Gauge 값을 안전하게 읽는 헬퍼."""
    try:
        if label_values:
            return metric.labels(**label_values)._value.get()
        return metric._value.get()
    except Exception:
        return 0.0


ALERT_RULES: list[AlertRule] = [
    AlertRule(
        name="HighActiveAgents",
        condition=lambda: sum(
            _safe_gauge_value(ACTIVE_AGENTS, {"phase": p})
            for p in ["DEFINE", "DESIGN", "BUILD",
                      "VERIFY", "DELIVER"]
        ) > 12,
        severity="WARNING",
        message="활성 에이전트 12개 초과 — Rate Limit 위험",
        cooldown=300,
    ),
    AlertRule(
        name="CircuitBreakerOpenAnthropic",
        condition=lambda: _safe_gauge_value(CB_STATE, {"provider": "anthropic"}) == 2,
        severity="CRITICAL",
        message="anthropic Circuit Breaker OPEN — 에이전트 실행 불가",
        cooldown=60,
    ),
    AlertRule(
        name="OutboxBacklog",
        condition=lambda: _safe_gauge_value(OUTBOX_QUEUE_DEPTH) > 100,
        severity="WARNING",
        message="Outbox 미처리 메시지 100개 초과",
        cooldown=120,
    ),
]


class AlertManager:
    """
    30초 주기로 ALERT_RULES 평가.
    발화 시 structlog + (선택적) 외부 채널.
    """

    CHECK_INTERVAL = 30  # 초

    def __init__(self, extra_rules: list[AlertRule] | None = None) -> None:
        self._rules = ALERT_RULES + (extra_rules or [])
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("alert_manager_started")
        while self._running:
            await self.check_and_fire()
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def check_and_fire(self) -> list[str]:
        """단일 체크 사이클. 반환: 발화된 rule 이름 목록."""
        fired = []
        for rule in self._rules:
            if rule.should_fire():
                logger.warning(
                    "alert_fired",
                    rule=rule.name,
                    severity=rule.severity,
                    message=rule.message,
                )
                rule.record_fired()
                fired.append(rule.name)
                # Slack webhook으로 알림 (background, 실패해도 무시)
                asyncio.create_task(
                    _post_slack(f"[V9 ALERT {rule.severity}] {rule.name}: {rule.message}")
                )
        return fired
