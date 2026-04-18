"""Dashboard drill-down query helpers (S2-3).

대시보드 7종 verification badge 클릭 시 노출할 상세 데이터를 SQL 한 곳에서
조립. frontend/router 는 이 모듈만 import 해서 JSON 변환.

Badge 카테고리 → 쿼리:
- build      — 빌드 통과/실패 로그 + 에러 메시지
- runtime    — 기동 성공·실패 노드
- e2e        — E2E 테스트 결과
- ui_test    — UI 컴포넌트 테스트
- visual     — 디자인 매칭 검증
- a11y       — 접근성 검사
- quality    — 종합 QA verdict

설계:
- 코어 무수정 — frontend 가 import 해서 JSON 응답 조립
- 각 카테고리는 (verifications 또는 nodes 또는 audit_log)에서 fetch
- 실패 시 빈 list 반환 (graceful)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# 카테고리 → (테이블, where 키워드)
_CATEGORY_QUERIES: dict[str, tuple[str, str]] = {
    "build":   ("nodes", "빌드"),
    "runtime": ("nodes", "기동"),
    "e2e":     ("nodes", "e2e"),
    "ui_test": ("nodes", "ui 테스트"),
    "visual":  ("nodes", "디자인 매칭"),
    "a11y":    ("nodes", "접근성"),
    "quality": ("nodes", "qa"),
}


async def fetch_drilldown(
    db: Any,
    engagement_id: str,
    category: str,
    limit: int = 50,
) -> list[dict]:
    """카테고리별 검증 결과 상세 fetch.

    Returns: [{node_id, name, state, updated_at, description, ...}]
    """
    if db is None:
        return []
    if category not in _CATEGORY_QUERIES:
        return []
    table, kw = _CATEGORY_QUERIES[category]
    try:
        if table == "nodes":
            rows = await db.fetchall(
                """SELECT id AS node_id, task_name, type, state,
                       updated_at, description, retry_count, stall_count
                FROM nodes
                WHERE engagement_id=?
                  AND LOWER(task_name) LIKE ?
                ORDER BY updated_at DESC LIMIT ?""",
                (engagement_id, f"%{kw.lower()}%", limit),
            )
        else:
            rows = []
        return [_normalize_row(dict(r)) for r in rows]
    except Exception as e:
        logger.warning("drilldown(%s) failed: %s", category, e)
        return []


def _normalize_row(row: dict) -> dict:
    """description JSON parse 시도 + 안전한 직렬화."""
    desc = row.get("description")
    if isinstance(desc, str) and desc.strip().startswith(("{", "[")):
        try:
            row["description_parsed"] = json.loads(desc)
        except Exception:
            row["description_parsed"] = None
    return row


async def fetch_summary(db: Any, engagement_id: str) -> dict:
    """대시보드 메인용 — 모든 카테고리 1건 요약 (count + last_updated)."""
    if db is None:
        return {}
    out: dict = {}
    for cat, (_, kw) in _CATEGORY_QUERIES.items():
        try:
            row = await db.fetchone(
                """SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN state='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN state IN ('FAILED','INVALID','SUSPENDED') THEN 1 ELSE 0 END) AS failed,
                    MAX(updated_at) AS last_updated
                FROM nodes
                WHERE engagement_id=? AND LOWER(task_name) LIKE ?""",
                (engagement_id, f"%{kw.lower()}%"),
            )
            out[cat] = dict(row) if row else {}
        except Exception as e:
            logger.warning("fetch_summary(%s) failed: %s", cat, e)
            out[cat] = {}
    return out


async def fetch_violations(
    db: Any, engagement_id: str, limit: int = 100,
) -> list[dict]:
    """phase contract 위반 이력 (S3-1 와 연동)."""
    if db is None:
        return []
    try:
        rows = await db.fetchall(
            """SELECT ts, phase, violations, summary
            FROM contract_violations
            WHERE engagement_id=? ORDER BY ts DESC LIMIT ?""",
            (engagement_id, limit),
        )
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            try:
                d["violations_parsed"] = json.loads(d.get("violations") or "[]")
            except Exception:
                d["violations_parsed"] = []
            out.append(d)
        return out
    except Exception as e:
        logger.warning("fetch_violations failed: %s", e)
        return []
