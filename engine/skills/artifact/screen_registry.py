"""Screen Registry — 화면 목록 정의서의 ID↔이름 매핑을 DB 에 구조화 저장.

Tier 2-B/C 구현. upstream artifact content 를 매 chunk_items 호출마다 regex 파싱하는
fragile 경로 제거. 정의서 저장 시점에 1회 추출 → DB UPSERT → 후속 skill 이 DB 조회.

재발 차단 범위:
  - state='COMPLETED' 조건 (정의서 IN_PROGRESS 여도 DB 에 last-known-good 유지)
  - artifact 구조 변화 (markdown 테이블 → definition list 등 LLM 변덕)
  - 타입 시그니처 혼용 (list[str] vs list[dict]) — 모든 소비처가 DB 스키마로 강제

추출 우선순위 (가장 안정 → 가장 약함):
  1. <!-- SCREEN_REGISTRY {json} --> block (Tier 2-A 포맷)
  2. markdown 테이블 | ID | 이름 | ... |
  3. regex ID only (최후 fallback)

graceful degrade:
  - 추출 실패 = 빈 list 리턴 (artifact 저장은 성공 유지)
  - sync 실패 = warning log (saver 흐름 방해 안 함)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SCREEN_ID_RE = re.compile(
    r"(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})",
    re.IGNORECASE,
)
_REGISTRY_BLOCK_RE = re.compile(
    r"<!--\s*SCREEN_REGISTRY\s*([\s\S]*?)-->",
    re.IGNORECASE,
)
_TABLE_ROW_RE = re.compile(
    r"\|\s*(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})\s*\|\s*([^|\n]+?)\s*\|",
    re.IGNORECASE,
)


def extract_screen_registry(content: str) -> list[dict]:
    """정의서 content 에서 ID↔이름↔메타데이터 매핑 추출.

    반환: [{"id": ..., "name": ..., "domain": ..., "priority": ..., "intent": ...}, ...]
    추출 실패 시 빈 list.
    """
    if not content:
        return []

    # 1. Tier 2-A JSON block 우선 (가장 안정 — LLM 이 compliance 하면)
    m = _REGISTRY_BLOCK_RE.search(content)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            screens = data.get("screens") or []
            if isinstance(screens, list) and screens:
                result: list[dict] = []
                for s in screens:
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get("id", "")).strip().upper()
                    name = str(s.get("name", "")).strip()
                    if not sid or not name:
                        continue
                    result.append({
                        "id": sid,
                        "name": name,
                        "domain": str(s.get("domain", "")).strip() or None,
                        "priority": str(s.get("priority", "")).strip() or None,
                        "intent": str(s.get("intent", "")).strip() or None,
                    })
                if result:
                    logger.info(
                        "screen_registry_extract_json_block screens=%d", len(result),
                    )
                    return result
        except Exception as e:
            logger.warning("screen_registry_json_parse_fail error=%s", str(e)[:100])

    # 2. Markdown 테이블 fallback
    seen: set[str] = set()
    table_rows: list[dict] = []
    for mt in _TABLE_ROW_RE.finditer(content):
        sid = mt.group(1).upper()
        name = re.sub(r"\s+", " ", mt.group(2)).strip()
        if sid in seen or not name or name.lower() in ("화면명", "name", "이름"):
            continue
        seen.add(sid)
        table_rows.append({
            "id": sid, "name": name,
            "domain": None, "priority": None, "intent": None,
        })
    if table_rows:
        logger.info("screen_registry_extract_table screens=%d", len(table_rows))
        return table_rows

    # 3. Regex ID only (이름 없이, 최후 fallback)
    id_only: list[dict] = []
    for mid in _SCREEN_ID_RE.finditer(content):
        sid = mid.group(1).upper()
        if sid in seen:
            continue
        seen.add(sid)
        id_only.append({
            "id": sid, "name": sid,  # 이름 없음 → ID 를 이름으로 임시
            "domain": None, "priority": None, "intent": None,
        })
    if id_only:
        logger.warning(
            "screen_registry_extract_id_only screens=%d (이름 정보 없음)",
            len(id_only),
        )
        return id_only

    return []


async def sync_screen_registry(
    db: Any, project_id: str, source_version: int | None, screens: list[dict],
) -> int:
    """UPSERT — 기존 (project_id, screen_id) 갱신, 새 행 추가.

    삭제된 ID 는 남겨둠 (audit — 이전 버전에 있었지만 현재 없는 화면 감지용).
    race condition 방지: BEGIN IMMEDIATE transaction + source_version 비교.

    Returns: UPSERT 한 row 수.
    """
    if not screens:
        return 0

    now_iso = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc,
    ).isoformat()

    affected = 0
    try:
        async with db.begin_immediate():
            for s in screens:
                sid = s.get("id")
                name = s.get("name")
                if not sid or not name:
                    continue
                # source_version 이 기존 row 보다 작으면 skip (stale write 방지)
                if source_version is not None:
                    existing = await db.fetchone(
                        "SELECT source_version FROM screen_registry "
                        "WHERE project_id=? AND screen_id=?",
                        (project_id, sid),
                    )
                    if existing and existing.get("source_version") is not None:
                        if (existing["source_version"] or 0) > source_version:
                            continue
                await db.execute(
                    "INSERT INTO screen_registry (project_id, screen_id, screen_name, "
                    "domain, priority, intent, source_version, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_id, screen_id) DO UPDATE SET "
                    "screen_name=excluded.screen_name, domain=excluded.domain, "
                    "priority=excluded.priority, intent=excluded.intent, "
                    "source_version=excluded.source_version, updated_at=excluded.updated_at",
                    (project_id, sid, name, s.get("domain"), s.get("priority"),
                     s.get("intent"), source_version, now_iso),
                )
                affected += 1
    except Exception as e:
        logger.warning(
            "screen_registry_sync_transaction_fail project=%s error=%s",
            project_id[:8] if project_id else "?", str(e)[:150],
        )
        return 0
    return affected


async def load_screen_registry(db: Any, project_id: str) -> list[dict]:
    """DB 에서 프로젝트의 screen_registry 전체 조회. 후속 chunk 소비처가 사용.

    Returns: [{"id": ..., "name": ..., "domain": ..., "priority": ..., "intent": ...}, ...]
    비어있으면 빈 list (fallback 경로 판단용).
    """
    if not db or not project_id:
        return []
    try:
        rows = await db.fetchall(
            "SELECT screen_id AS id, screen_name AS name, domain, priority, intent "
            "FROM screen_registry WHERE project_id=? ORDER BY screen_id",
            (project_id,),
        )
        return [
            {"id": r["id"], "name": r["name"],
             "domain": r.get("domain"), "priority": r.get("priority"),
             "intent": r.get("intent")}
            for r in rows
        ]
    except Exception as e:
        logger.warning(
            "screen_registry_load_fail project=%s error=%s",
            project_id[:8] if project_id else "?", str(e)[:120],
        )
        return []
