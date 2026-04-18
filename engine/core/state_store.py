"""
Stage 3: Atomic State Store — Map-Reduce 된 task 단위 idempotent 상태 저장.

DAGAdvancer 와 state_machine 은 노드 레벨 상태만 관리.
chunk 단위 진행 상태(item 별 COMPLETE/FAILED/NEEDS_HUMAN) 는 이 StateStore 가 담당.

사용 패턴 (engine/skills/executor.py _chunked_*_items_generate 내부에서):

    store = AtomicStateStore(db)
    for item in chunk_items:
        if not await store.reserve(eng_id, node.id, item):
            continue  # 이미 처리됨
        try:
            artifact = await model_adapter.call(...)
            await store.complete(eng_id, node.id, item, hash(artifact))
        except Exception as e:
            await store.fail(eng_id, node.id, item, str(e))

    incomplete = await store.list_incomplete(eng_id, node.id, expected_items)
    if incomplete:
        # coverage.py retry_missing 로 위임
        ...
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AtomicStateStore:
    """task 단위 idempotent reserve/complete/fail 상태 저장소.

    SQLite WAL 모드 의존. 동일 (engagement_id, node_id, item_key) PK 로
    중복 예약/저장 방지 (OR IGNORE + 조회 기반).
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lock = asyncio.Lock()  # 같은 프로세스 내 race 완전 차단용

    async def reserve(
        self, engagement_id: str, node_id: str, item_key: str,
    ) -> bool:
        """이미 처리됐거나 예약된 item 이면 False, 신규면 RESERVED 기록 후 True.

        동시 reserve 시나리오: PRIMARY KEY 제약 + INSERT OR IGNORE 로 1회 성공 보장.
        """
        async with self._lock:
            # 먼저 현 상태 조회
            row = await self._db.fetchone(
                """SELECT status FROM atomic_state
                   WHERE engagement_id=? AND node_id=? AND item_key=?""",
                (engagement_id, node_id, item_key),
            )
            if row:
                status = row["status"]
                if status in ("RESERVED", "COMPLETE"):
                    return False  # 다른 워커가 처리 중 or 완료
                if status in ("FAILED", "PENDING"):
                    # 재시도 허용 — RESERVED 로 전이
                    await self._db.execute(
                        """UPDATE atomic_state
                           SET status='RESERVED', updated_at=?
                           WHERE engagement_id=? AND node_id=? AND item_key=?""",
                        (_now(), engagement_id, node_id, item_key),
                    )
                    return True
                # NEEDS_HUMAN / SKIPPED 은 자동 reserve 대상 아님
                return False

            # 신규 — INSERT
            try:
                await self._db.execute(
                    """INSERT INTO atomic_state
                       (engagement_id, node_id, item_key, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'RESERVED', ?, ?)""",
                    (engagement_id, node_id, item_key, _now(), _now()),
                )
                return True
            except Exception as e:
                # 동시 insert race — 다른 워커가 선점
                logger.debug(
                    "atomic_reserve_race eng=%s node=%s item=%s err=%s",
                    engagement_id[:8], node_id[:8], item_key, str(e)[:80],
                )
                return False

    async def complete(
        self, engagement_id: str, node_id: str, item_key: str,
        artifact_hash: str | None = None,
    ) -> None:
        """성공 기록. artifact_hash 은 감사·재사용 목적."""
        await self._db.execute(
            """UPDATE atomic_state
               SET status='COMPLETE', artifact_hash=?, updated_at=?, reason=NULL
               WHERE engagement_id=? AND node_id=? AND item_key=?""",
            (artifact_hash, _now(), engagement_id, node_id, item_key),
        )

    async def fail(
        self, engagement_id: str, node_id: str, item_key: str, reason: str,
    ) -> None:
        """실패 기록 — retry_count 증가."""
        await self._db.execute(
            """UPDATE atomic_state
               SET status='FAILED', retry_count=retry_count+1,
                   reason=?, updated_at=?
               WHERE engagement_id=? AND node_id=? AND item_key=?""",
            (reason[:500], _now(), engagement_id, node_id, item_key),
        )

    async def mark_needs_human(
        self, engagement_id: str, node_id: str, item_key: str, reason: str,
    ) -> None:
        """자동 재시도 한도 도달 → 사람 개입 필요."""
        await self._db.execute(
            """UPDATE atomic_state
               SET status='NEEDS_HUMAN', reason=?, updated_at=?
               WHERE engagement_id=? AND node_id=? AND item_key=?""",
            (reason[:500], _now(), engagement_id, node_id, item_key),
        )

    async def mark_skipped(
        self, engagement_id: str, node_id: str, item_key: str, reason: str,
    ) -> None:
        """운영자가 명시적으로 포기 (coverage 에서 제외)."""
        await self._db.execute(
            """INSERT INTO atomic_state
                 (engagement_id, node_id, item_key, status, reason, created_at, updated_at)
               VALUES (?, ?, ?, 'SKIPPED', ?, ?, ?)
               ON CONFLICT(engagement_id, node_id, item_key) DO UPDATE SET
                 status='SKIPPED', reason=excluded.reason, updated_at=excluded.updated_at""",
            (engagement_id, node_id, item_key, reason[:500], _now(), _now()),
        )

    async def get_status(
        self, engagement_id: str, node_id: str, item_key: str,
    ) -> str | None:
        row = await self._db.fetchone(
            """SELECT status FROM atomic_state
               WHERE engagement_id=? AND node_id=? AND item_key=?""",
            (engagement_id, node_id, item_key),
        )
        return row["status"] if row else None

    async def get_retry_count(
        self, engagement_id: str, node_id: str, item_key: str,
    ) -> int:
        row = await self._db.fetchone(
            """SELECT retry_count FROM atomic_state
               WHERE engagement_id=? AND node_id=? AND item_key=?""",
            (engagement_id, node_id, item_key),
        )
        return int(row["retry_count"]) if row else 0

    async def list_by_status(
        self, engagement_id: str, node_id: str, status: str,
    ) -> list[str]:
        rows = await self._db.fetchall(
            """SELECT item_key FROM atomic_state
               WHERE engagement_id=? AND node_id=? AND status=?""",
            (engagement_id, node_id, status),
        )
        return [r["item_key"] for r in rows]

    async def list_complete(
        self, engagement_id: str, node_id: str,
    ) -> list[str]:
        return await self.list_by_status(engagement_id, node_id, "COMPLETE")

    async def list_incomplete(
        self, engagement_id: str, node_id: str, expected: list[str],
    ) -> list[str]:
        """expected 중 COMPLETE 상태 아닌 항목 반환 (coverage 용).

        SKIPPED 는 의도적 제외 → incomplete 아님.
        """
        if not expected:
            return []
        rows = await self._db.fetchall(
            """SELECT item_key, status FROM atomic_state
               WHERE engagement_id=? AND node_id=?""",
            (engagement_id, node_id),
        )
        done = {
            r["item_key"] for r in rows
            if r["status"] in ("COMPLETE", "SKIPPED")
        }
        return [it for it in expected if it not in done]

    async def summary(
        self, engagement_id: str, node_id: str,
    ) -> dict[str, int]:
        """status 별 카운트 — Flow 뷰 사이드패널용."""
        rows = await self._db.fetchall(
            """SELECT status, COUNT(*) AS cnt FROM atomic_state
               WHERE engagement_id=? AND node_id=? GROUP BY status""",
            (engagement_id, node_id),
        )
        return {r["status"]: int(r["cnt"]) for r in rows}
