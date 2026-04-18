"""
Stage 8: Content Hash Cache — input hash 동일 task 의 artifact 재사용.

재시도·유사 프로젝트 간 중복 작업 제거. OAuth 쿼터 절감의 핵심 레버.

Namespace 전략:
  1차 lookup: `{engagement_id}:{node_type}:{input_hash}`  (프로젝트 격리)
  2차 lookup: `*:{node_type}:{input_hash}`                (프로젝트 간 재사용 — 옵션)

Invalidation:
- PRD 변경 → PRD hash 가 input_hash 재료이므로 자연 무효화
- 명시 삭제: invalidate_namespace(engagement_id)

Feature flag: V8_CONTENT_CACHE=0 → 항상 MISS (bypass)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_CONTENT_CACHE", "1") != "0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContentHashCache:
    """input hash 기반 artifact 재사용 저장소.

    input hash 는 cache-stable 입력 블록들을 canonicalize JSON → SHA-256.
    출력 artifact 를 그대로 저장 후 동일 key 조회 시 반환.
    """

    def __init__(self, db: Any, enabled: bool | None = None) -> None:
        self._db = db
        self._enabled = _ENABLED if enabled is None else enabled

    @staticmethod
    def compute_hash(input_blocks: dict | list | str) -> str:
        """input blocks 를 canonical JSON 직렬화 후 SHA-256.

        dict 는 sort_keys=True 로 순서 독립. list/str 도 JSON 인코딩.
        """
        blob = json.dumps(input_blocks, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _key(self, namespace: str, input_hash: str) -> str:
        return f"{namespace}:{input_hash}"

    async def get(
        self,
        engagement_id: str,
        node_type: str,
        input_hash: str,
        allow_cross_engagement: bool = False,
    ) -> dict | None:
        """캐시 조회. hit 시 content + metadata, miss 시 None.

        allow_cross_engagement=True 면 다른 engagement 도 포함 (node_type + input_hash 일치).
        """
        if not self._enabled:
            return None

        # 1차: engagement 격리 lookup
        namespace = f"{engagement_id}:{node_type}"
        key = self._key(namespace, input_hash)
        row = await self._db.fetchone(
            """SELECT content, input_tokens, output_tokens, hit_count, created_at
               FROM content_cache WHERE cache_key=?""",
            (key,),
        )
        if row:
            await self._record_hit(key)
            logger.info(
                "content_cache_hit scope=engagement node_type=%s input_hash=%s",
                node_type, input_hash[:12],
            )
            return dict(row)

        if not allow_cross_engagement:
            return None

        # 2차: node_type + input_hash cross-engagement lookup
        row = await self._db.fetchone(
            """SELECT content, input_tokens, output_tokens, hit_count, cache_key
               FROM content_cache
               WHERE node_type=? AND input_hash=?
               ORDER BY hit_count DESC, created_at DESC LIMIT 1""",
            (node_type, input_hash),
        )
        if row:
            await self._record_hit(row["cache_key"])
            logger.info(
                "content_cache_hit scope=cross node_type=%s input_hash=%s",
                node_type, input_hash[:12],
            )
            return dict(row)

        return None

    async def put(
        self,
        engagement_id: str,
        node_type: str,
        input_hash: str,
        content: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """artifact 저장. 동일 key 존재 시 덮어씀 (최신 결과 유지)."""
        if not self._enabled:
            return

        namespace = f"{engagement_id}:{node_type}"
        key = self._key(namespace, input_hash)
        await self._db.execute(
            """INSERT INTO content_cache
                 (cache_key, namespace, node_type, input_hash, content,
                  input_tokens, output_tokens, created_at, hit_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(cache_key) DO UPDATE SET
                 content=excluded.content,
                 input_tokens=excluded.input_tokens,
                 output_tokens=excluded.output_tokens,
                 created_at=excluded.created_at""",
            (key, namespace, node_type, input_hash, content,
             input_tokens, output_tokens, _now()),
        )

    async def _record_hit(self, cache_key: str) -> None:
        await self._db.execute(
            """UPDATE content_cache
               SET hit_count=hit_count+1, last_hit_at=?
               WHERE cache_key=?""",
            (_now(), cache_key),
        )

    async def invalidate_namespace(self, engagement_id: str) -> int:
        """engagement 전체 캐시 무효화 — PRD 변경 등."""
        result = await self._db.execute(
            "DELETE FROM content_cache WHERE namespace LIKE ?",
            (f"{engagement_id}:%",),
        )
        # execute 반환이 adapter 마다 다름 — rowcount 확보 어려우면 0 반환
        try:
            return int(result) if isinstance(result, int) else 0
        except Exception:
            return 0

    async def stats(self) -> dict:
        """전체 캐시 통계 — 지표 대시보드용."""
        row = await self._db.fetchone(
            """SELECT
                 COUNT(*) AS entries,
                 COALESCE(SUM(hit_count), 0) AS total_hits,
                 COALESCE(SUM(input_tokens), 0) AS total_input_saved,
                 COALESCE(SUM(output_tokens), 0) AS total_output_saved
               FROM content_cache"""
        )
        return dict(row) if row else {
            "entries": 0, "total_hits": 0,
            "total_input_saved": 0, "total_output_saved": 0,
        }
