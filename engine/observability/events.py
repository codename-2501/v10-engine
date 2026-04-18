"""Event aggregation (S2-1).

엔진 전반에 흩어진 logger.info("X_pass"/"X_fail") 호출을 집계해 "어떤 조치가
몇 번 발동·성공·실패했는지" 정량화. SQLite 기반 가벼운 카운터 + last-seen.

설계 원칙:
- best-effort: DB 실패해도 호출자에 예외 전파 X (logger.warning 만)
- 동기 호출 가능 (asyncio.create_task로 fire-and-forget) — 핫패스에 부담 X
- 30일 rolling: log_event 시 자동으로 30일 전 row purge
- 코어 파일 무수정 — 호출부가 점진적으로 import 해서 사용

스키마:
    event_counts(
        event_name TEXT,
        project_id TEXT,           -- NULL = global
        count INTEGER,
        last_at TEXT,
        first_at TEXT,
        last_payload TEXT,         -- 마지막 호출의 payload JSON (디버그)
        PRIMARY KEY (event_name, project_id)
    )

사용 예:
    from engine.observability.events import log_event

    await log_event(db, "chunked_doc_outline_ids", project_id=node.project_id,
                    payload={"node": node.id[:8], "count": 47})
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_counts (
    event_name TEXT NOT NULL,
    project_id TEXT,
    count INTEGER NOT NULL DEFAULT 0,
    last_at TEXT,
    first_at TEXT,
    last_payload TEXT,
    PRIMARY KEY (event_name, project_id)
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_event_counts_last_at
ON event_counts(last_at DESC)
"""


_schema_initialized = False


async def _ensure_schema(db: Any) -> None:
    """idempotent — 첫 호출에만 CREATE TABLE."""
    global _schema_initialized
    if _schema_initialized:
        return
    try:
        await db.execute(_SCHEMA)
        await db.execute(_INDEX)
        _schema_initialized = True
    except Exception as e:
        logger.warning("event_counts schema init failed: %s", e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def log_event(
    db: Any,
    event_name: str,
    project_id: str | None = None,
    payload: dict | None = None,
) -> None:
    """이벤트 1건 카운트 증가 + last_payload 갱신.

    DB 실패는 swallow — 핫패스에서 본 로직 보호 우선.
    """
    if db is None:
        return
    try:
        await _ensure_schema(db)
        now = _now()
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        # UPSERT — SQLite 3.24+
        await db.execute(
            """
            INSERT INTO event_counts(event_name, project_id, count, last_at, first_at, last_payload)
            VALUES(?, ?, 1, ?, ?, ?)
            ON CONFLICT(event_name, project_id) DO UPDATE SET
                count = count + 1,
                last_at = excluded.last_at,
                last_payload = excluded.last_payload
            """,
            (event_name, project_id, now, now, payload_json),
        )
    except Exception as e:
        logger.warning("log_event(%s) failed: %s", event_name, e)


async def fetch_recent(
    db: Any,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """최근 발동 이벤트 N개. 대시보드용."""
    try:
        await _ensure_schema(db)
        if project_id is not None:
            rows = await db.fetchall(
                """SELECT event_name, count, last_at, first_at, last_payload
                FROM event_counts WHERE project_id=? OR project_id IS NULL
                ORDER BY last_at DESC LIMIT ?""",
                (project_id, limit),
            )
        else:
            rows = await db.fetchall(
                """SELECT event_name, project_id, count, last_at, first_at, last_payload
                FROM event_counts ORDER BY last_at DESC LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("fetch_recent failed: %s", e)
        return []


async def purge_old(db: Any, days: int = 30) -> int:
    """30일 이전 row 삭제. 정기 호출 (watchdog 등)."""
    try:
        await _ensure_schema(db)
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cursor = await db.execute(
            "DELETE FROM event_counts WHERE last_at < ?", (cutoff_iso,),
        )
        return getattr(cursor, "rowcount", 0) or 0
    except Exception as e:
        logger.warning("purge_old failed: %s", e)
        return 0
