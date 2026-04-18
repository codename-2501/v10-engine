"""Audit log (S3-5).

노드 상태 전이·재시도·cascade·artifact write 등 모든 의미있는 이벤트의
who/when/what/before/after 기록. 사고 시 원인 추적 + 협업 안전.

스키마:
    audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT,             -- 'engine' | 'user:xxx' | 'watchdog' | 'cascade'
        action TEXT NOT NULL,   -- 'state_transition' | 'retry' | 'cascade_invalidate' | ...
        node_id TEXT,
        engagement_id TEXT,
        before TEXT,            -- JSON snapshot
        after TEXT,             -- JSON snapshot
        meta TEXT               -- 추가 컨텍스트 JSON
    )

설계:
- best-effort: 로깅 실패가 본 로직 차단 X
- 코어 파일 무수정 — call site들이 import 후 점진 추가
- 조회 인덱스: (engagement_id, ts), (node_id, ts), (action, ts)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    node_id TEXT,
    engagement_id TEXT,
    before TEXT,
    after TEXT,
    meta TEXT
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_engagement_ts ON audit_log(engagement_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_node_ts ON audit_log(node_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_log(action, ts DESC)",
]


_schema_initialized = False


async def _ensure_schema(db: Any) -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    try:
        await db.execute(_SCHEMA)
        for ix in _INDEXES:
            await db.execute(ix)
        _schema_initialized = True
    except Exception as e:
        logger.warning("audit_log schema init failed: %s", e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)[:500]


async def record(
    db: Any,
    action: str,
    *,
    actor: str = "engine",
    node_id: str | None = None,
    engagement_id: str | None = None,
    before: Any = None,
    after: Any = None,
    meta: Any = None,
) -> None:
    """Audit row 1건 기록. swallow on failure."""
    if db is None:
        return
    try:
        await _ensure_schema(db)
        await db.execute(
            """INSERT INTO audit_log(ts, actor, action, node_id, engagement_id, before, after, meta)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(), actor, action, node_id, engagement_id,
                _safe_json(before), _safe_json(after), _safe_json(meta),
            ),
        )
    except Exception as e:
        logger.warning("audit_log.record(%s) failed: %s", action, e)


async def query_engagement(
    db: Any, engagement_id: str, limit: int = 200,
) -> list[dict]:
    """Engagement 의 최근 audit 항목."""
    try:
        await _ensure_schema(db)
        rows = await db.fetchall(
            """SELECT ts, actor, action, node_id, before, after, meta
            FROM audit_log WHERE engagement_id=? ORDER BY ts DESC LIMIT ?""",
            (engagement_id, limit),
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_engagement failed: %s", e)
        return []


async def query_node(db: Any, node_id: str, limit: int = 100) -> list[dict]:
    try:
        await _ensure_schema(db)
        rows = await db.fetchall(
            """SELECT ts, actor, action, before, after, meta
            FROM audit_log WHERE node_id=? ORDER BY ts DESC LIMIT ?""",
            (node_id, limit),
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_node failed: %s", e)
        return []
