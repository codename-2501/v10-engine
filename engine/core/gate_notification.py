"""
engine/core/gate_notification.py
GateNotificationService — Outbox 재사용 + 지수 백오프 재시도.
별도 notification_deliveries 테이블 없음.
outbox.destination='NOTIFICATION' 재사용.
NotificationWorker: asyncio.sleep 루프 + 5단계 지수 백오프.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from engine.db.adapter import DatabaseAdapter
from engine.security.crypto import AES256GCM

logger = logging.getLogger(__name__)


class GateNotificationService:
    """
    Gate 이벤트 발생 시 notification_subscriptions 조회 → Outbox INSERT.
    DB 트랜잭션과 원자적 (Outbox 패턴).
    """

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    async def notify(
        self,
        event_type: str,
        engagement_id: str,
        payload: dict,
    ) -> list[str]:
        """
        구독자 조회 → Outbox 추가.
        반환: 생성된 outbox_id 목록.
        """
        subs = await self._db.fetchall(
            """SELECT id, channel_type FROM notification_subscriptions
               WHERE engagement_id=? AND event_type=? AND is_active=1""",
            (engagement_id, event_type),
        )
        if not subs:
            return []

        now = _now()
        event_id = str(uuid.uuid4())

        # event_store에 이벤트 기록
        await self._insert_event(event_id, event_type, engagement_id, payload, now)

        outbox_ids = []
        for sub in subs:
            outbox_id = str(uuid.uuid4())
            full_payload = json.dumps({
                "subscription_id": sub["id"],
                "channel_type": sub["channel_type"],
                **payload,
            }, ensure_ascii=False)

            await self._db.execute(
                """INSERT INTO outbox
                   (id, event_id, event_type, payload, destination,
                    status, retry_count, max_retries, next_retry_at, created_at)
                   VALUES (?, ?, ?, ?, 'NOTIFICATION', 'PENDING', 0, 5, ?, ?)""",
                (outbox_id, event_id, event_type, full_payload, now, now),
            )
            outbox_ids.append(outbox_id)

        logger.info(
            "gate_notification_queued event_type=%s engagement_id=%s count=%s",
            event_type,
            engagement_id,
            len(outbox_ids),
        )
        return outbox_ids

    async def _insert_event(
        self,
        event_id: str,
        event_type: str,
        engagement_id: str,
        payload: dict,
        now: str,
    ) -> None:
        payload_str = json.dumps(payload, ensure_ascii=False)
        checksum = hashlib.sha256(
            f"{event_id}{event_type}{payload_str}".encode()
        ).hexdigest()
        await self._db.execute(
            """INSERT INTO event_store
               (event_id, aggregate_type, aggregate_id, event_type,
                payload, checksum, engagement_id, actor, recorded_at)
               VALUES (?, 'Engagement', ?, ?, ?, ?, ?, 'SYSTEM', ?)""",
            (event_id, engagement_id, event_type, payload_str, checksum, engagement_id, now),
        )


class NotificationWorker:
    """
    asyncio.sleep 루프로 Outbox(destination='NOTIFICATION') 폴링.
    5단계 지수 백오프: 30/60/120/300/600초.
    DEAD_LETTERED: max_retries 초과 시.
    """

    RETRY_DELAYS = [30, 60, 120, 300, 600]  # 초
    POLL_INTERVAL = 5   # 초
    BATCH_SIZE    = 10

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("notification_worker_started")
        while self._running:
            try:
                pending = await self._fetch_pending()
                for msg in pending:
                    await self._deliver(msg)
            except Exception as exc:
                logger.error("notification_worker_error error=%s", str(exc))
            await asyncio.sleep(self.POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _fetch_pending(self) -> list[dict]:
        return await self._db.fetchall(
            """SELECT o.id, o.event_id, o.event_type, o.payload,
                      o.retry_count, o.max_retries,
                      ns.channel_type,
                      ns.encrypted_endpoint,
                      ns.encrypted_recipient
               FROM outbox o
               LEFT JOIN notification_subscriptions ns
                 ON json_extract(o.payload, '$.subscription_id') = ns.id
               WHERE o.destination='NOTIFICATION'
                 AND o.status IN ('PENDING', 'FAILED')
                 AND (o.next_retry_at IS NULL
                      OR o.next_retry_at <= datetime('now'))
               LIMIT ?""",
            (self.BATCH_SIZE,),
        )

    async def _deliver(self, msg: dict) -> None:
        try:
            channel = msg.get("channel_type", "LOG_ONLY")
            if channel == "WEBHOOK" and msg.get("encrypted_endpoint"):
                await self._send_webhook(msg)
            elif channel == "EMAIL" and msg.get("encrypted_recipient"):
                await self._send_email(msg)
            else:
                # LOG_ONLY 또는 정보 없음 → 로그만
                logger.info("notification_log_only event_type=%s", msg["event_type"])

            await self._db.execute(
                "UPDATE outbox SET status='DELIVERED', processed_at=? WHERE id=?",
                (_now(), msg["id"]),
            )
        except Exception as exc:
            await self._handle_failure(msg, exc)

    async def _handle_failure(self, msg: dict, exc: Exception) -> None:
        retry_count = (msg["retry_count"] or 0) + 1
        max_retries = msg["max_retries"] or 5

        if retry_count >= max_retries:
            await self._db.execute(
                """UPDATE outbox
                   SET status='DEAD_LETTERED', error_message=?, retry_count=?
                   WHERE id=?""",
                (str(exc)[:500], retry_count, msg["id"]),
            )
            logger.error(
                "notification_dead_lettered outbox_id=%s event_type=%s error=%s",
                msg["id"],
                msg["event_type"],
                str(exc),
            )
        else:
            delay = self.RETRY_DELAYS[min(retry_count - 1, len(self.RETRY_DELAYS) - 1)]
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            await self._db.execute(
                """UPDATE outbox
                   SET status='FAILED', retry_count=?, next_retry_at=?, error_message=?
                   WHERE id=?""",
                (retry_count, next_retry, str(exc)[:500], msg["id"]),
            )
            logger.warning(
                "notification_retry_scheduled outbox_id=%s retry_count=%s delay=%s",
                msg["id"],
                retry_count,
                delay,
            )

    async def _send_webhook(self, msg: dict) -> None:
        """WEBHOOK: AES 복호화 후 POST."""
        try:
            encrypt_key = AES256GCM.key_from_env()
            url = AES256GCM.decrypt(msg["encrypted_endpoint"], encrypt_key)
        except Exception as exc:
            raise RuntimeError(f"웹훅 URL 복호화 실패: {exc}") from exc

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=json.loads(msg["payload"]),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"웹훅 응답 오류: {resp.status}")

    async def _send_email(self, msg: dict) -> None:
        """EMAIL: 현재 LOG_ONLY 폴백 (SMTP 미구현 — 향후 확장)."""
        logger.info(
            "notification_email_skipped outbox_id=%s reason=%s",
            msg["id"],
            "SMTP 미구현 — LOG_ONLY 폴백",
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
