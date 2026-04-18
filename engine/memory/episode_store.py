"""
engine/memory/episode_store.py
에피소드 기반 벡터 메모리.

실패 패턴을 의미 유사도로 검색해 프롬프트에 관련 경고를 주입한다.
임베딩 생성은 항상 백그라운드 태스크 — 실패해도 에피소드 저장에는 영향 없음.

Phase F-3 통합:
- save_episode(): episodes 테이블에 INSERT 후 백그라운드 임베딩
- search_similar_episodes(): episodes 테이블 벡터 검색 (executor/habits 공통)
- search_similar_gotchas(): project_gotchas 벡터 검색 (기존 호환)
- enrich_gotcha_embedding(): project_gotchas.embedding_json 비동기 갱신
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EpisodeStore:
    """에피소드 저장 + 의미 유사도 검색."""

    def __init__(self, db: Any) -> None:
        self._db = db

    # ── 에피소드 저장 ──────────────────────────────────────────

    async def save_episode(
        self,
        project_id: str,
        node_id: str,
        node_name: str,
        episode_type: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """에피소드 INSERT. 임베딩은 백그라운드 처리."""
        episode_id = str(uuid.uuid4())
        trimmed_content = content[:2000]
        try:
            await self._db.execute(
                """INSERT INTO episodes
                   (id, project_id, node_id, node_name, episode_type,
                    content, metadata_json, embedding_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    episode_id,
                    project_id,
                    node_id,
                    node_name,
                    episode_type,
                    trimmed_content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            asyncio.create_task(
                self._embed_episode_with_retry(episode_id, trimmed_content),
                name=f"embed-ep-{episode_id[:8]}",
            )
        except Exception as e:
            logger.warning(
                "episode_save_failed node=%s: %s",
                node_id[:8] if node_id else "?",
                e,
            )
        return episode_id

    async def _embed_episode_with_retry(
        self,
        episode_id: str,
        content: str,
        max_retries: int = 3,
    ) -> None:
        """
        임베딩 생성 재시도. 지수 백오프(1s, 2s, 4s). 모두 실패 시 WARNING 로그.
        실패한 에피소드는 embedding_json=NULL 유지 → 검색 시 keyword fallback 동작.
        """
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                from engine.ai.embedding_adapter import get_embedding_provider
                provider = get_embedding_provider()
                vectors = await asyncio.to_thread(provider.encode, [content])
                await self._db.execute(
                    "UPDATE episodes SET embedding_json=? WHERE id=?",
                    (json.dumps(vectors[0]), episode_id),
                )
                return
            except Exception as e:
                last_err = e
                logger.debug(
                    "episode_embed_attempt=%d id=%s: %s",
                    attempt + 1, episode_id[:8], e,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        # 모든 재시도 실패 → WARN (debug보다 상위, 운영자 가시)
        logger.warning(
            "episode_embed_exhausted id=%s attempts=%d last_error=%s",
            episode_id[:8], max_retries, last_err,
        )

    # ── 에피소드 벡터 검색 (Phase F 핵심) ──────────────────

    async def search_similar_episodes(
        self,
        query: str,
        project_id: str,
        episode_type: str | None = None,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        """
        episodes 테이블에서 의미 유사도 기반 검색.
        episode_type=None 이면 전체 타입(gotcha/success/pattern)에서 검색.
        임베딩 없는 행은 최신순으로 보충 (total top_k).
        """
        try:
            from engine.ai.embedding_adapter import (
                get_embedding_provider,
                cosine_similarity,
            )
            provider = get_embedding_provider()
            q_vecs = await asyncio.to_thread(provider.encode, [query])
            q_vec = q_vecs[0]
        except Exception as e:
            logger.debug("episode_query_embed_failed: %s — keyword fallback", e)
            return await self._episode_keyword_fallback(project_id, episode_type, top_k)

        try:
            if episode_type:
                rows = await self._db.fetchall(
                    """SELECT id, node_id, node_name, episode_type,
                              content, metadata_json, embedding_json, created_at
                       FROM episodes
                       WHERE project_id=? AND episode_type=?
                       ORDER BY created_at DESC LIMIT 200""",
                    (project_id, episode_type),
                )
            else:
                rows = await self._db.fetchall(
                    """SELECT id, node_id, node_name, episode_type,
                              content, metadata_json, embedding_json, created_at
                       FROM episodes
                       WHERE project_id=?
                       ORDER BY created_at DESC LIMIT 200""",
                    (project_id,),
                )
        except Exception as e:
            logger.debug("episode_fetch_failed: %s", e)
            return []

        scored: list[tuple[float, dict]] = []
        no_embed: list[dict] = []

        for row in rows:
            d = dict(row)
            emb = d.get("embedding_json")
            if emb:
                try:
                    vec = json.loads(emb)
                    sim = cosine_similarity(q_vec, vec)
                    if sim >= min_similarity:
                        scored.append((sim, d))
                except Exception:
                    no_embed.append(d)
            else:
                no_embed.append(d)

        scored.sort(key=lambda x: -x[0])
        result = [item for _, item in scored[:top_k]]

        # 임베딩 없는 최신 항목으로 보충
        remaining = top_k - len(result)
        if remaining > 0:
            existing_ids = {r["id"] for r in result}
            for d in no_embed:
                if remaining <= 0:
                    break
                if d["id"] not in existing_ids:
                    result.append(d)
                    remaining -= 1

        return result

    async def _episode_keyword_fallback(
        self,
        project_id: str,
        episode_type: str | None,
        top_k: int,
    ) -> list[dict]:
        """임베딩 실패 시 최신순 fallback."""
        try:
            if episode_type:
                rows = await self._db.fetchall(
                    """SELECT id, node_id, node_name, episode_type,
                              content, metadata_json, NULL AS embedding_json, created_at
                       FROM episodes
                       WHERE project_id=? AND episode_type=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (project_id, episode_type, top_k),
                )
            else:
                rows = await self._db.fetchall(
                    """SELECT id, node_id, node_name, episode_type,
                              content, metadata_json, NULL AS embedding_json, created_at
                       FROM episodes
                       WHERE project_id=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (project_id, top_k),
                )
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Gotcha 벡터 검색 (기존 호환) ─────────────────────────

    async def search_similar_gotchas(
        self,
        query: str,
        project_id: str,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        """project_gotchas 의미 유사도 검색. 임베딩 없는 행은 최신순 보충."""
        try:
            from engine.ai.embedding_adapter import (
                get_embedding_provider,
                cosine_similarity,
            )
            provider = get_embedding_provider()
            q_vecs = await asyncio.to_thread(provider.encode, [query])
            q_vec = q_vecs[0]
        except Exception as e:
            logger.debug("gotcha_query_embed_failed: %s — keyword fallback", e)
            return await self._gotcha_keyword_fallback(project_id, top_k)

        try:
            rows = await self._db.fetchall(
                """SELECT id, category, description, source_node_name, embedding_json
                   FROM project_gotchas
                   WHERE project_id=?
                   ORDER BY created_at DESC LIMIT 100""",
                (project_id,),
            )
        except Exception as e:
            logger.debug("gotcha_fetch_failed: %s", e)
            return []

        scored: list[tuple[float, dict]] = []
        no_embed: list[dict] = []

        for row in rows:
            d = dict(row)
            emb = d.get("embedding_json")
            if emb:
                try:
                    vec = json.loads(emb)
                    sim = cosine_similarity(q_vec, vec)
                    if sim >= min_similarity:
                        scored.append((sim, d))
                except Exception:
                    no_embed.append(d)
            else:
                no_embed.append(d)

        scored.sort(key=lambda x: -x[0])
        result = [item for _, item in scored[:top_k]]

        remaining = top_k - len(result)
        if remaining > 0:
            existing_ids = {r["id"] for r in result}
            for d in no_embed:
                if remaining <= 0:
                    break
                if d["id"] not in existing_ids:
                    result.append(d)
                    remaining -= 1

        return result

    async def enrich_gotcha_embedding(self, gotcha_id: str, description: str) -> None:
        """project_gotchas.embedding_json 비동기 갱신. 실패 시 silent."""
        try:
            from engine.ai.embedding_adapter import get_embedding_provider
            provider = get_embedding_provider()
            vecs = await asyncio.to_thread(provider.encode, [description])
            await self._db.execute(
                "UPDATE project_gotchas SET embedding_json=? WHERE id=?",
                (json.dumps(vecs[0]), gotcha_id),
            )
        except Exception as e:
            logger.debug("gotcha_embed_failed id=%s: %s", gotcha_id[:8], e)

    async def _gotcha_keyword_fallback(
        self, project_id: str, top_k: int
    ) -> list[dict]:
        try:
            rows = await self._db.fetchall(
                """SELECT id, category, description, source_node_name,
                          NULL AS embedding_json
                   FROM project_gotchas
                   WHERE project_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (project_id, top_k),
            )
            return [dict(r) for r in rows]
        except Exception:
            return []
