"""Stage 8 ContentHashCache 테스트 (D8 Test Suite)."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine.db.adapter import SQLiteAdapter
from engine.core.content_cache import ContentHashCache


@pytest.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE content_cache (
          cache_key TEXT PRIMARY KEY,
          namespace TEXT NOT NULL,
          node_type TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          content TEXT NOT NULL,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          hit_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          last_hit_at TEXT
        );
    """)
    conn.close()
    yield SQLiteAdapter(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


def test_compute_hash_결정성():
    h1 = ContentHashCache.compute_hash({"a": 1, "b": 2})
    h2 = ContentHashCache.compute_hash({"b": 2, "a": 1})  # 키 순서 다름
    assert h1 == h2  # sort_keys → 동일 해시


def test_compute_hash_다른_내용_다른_해시():
    h1 = ContentHashCache.compute_hash({"a": 1})
    h2 = ContentHashCache.compute_hash({"a": 2})
    assert h1 != h2


@pytest.mark.asyncio
async def test_put_후_get_hit(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-a", "<section>a</section>",
                    input_tokens=100, output_tokens=50)
    hit = await cache.get("e1", "ui_section", "hash-a")
    assert hit is not None
    assert hit["content"] == "<section>a</section>"


@pytest.mark.asyncio
async def test_get_miss(db):
    cache = ContentHashCache(db)
    miss = await cache.get("e1", "ui_section", "no-such-hash")
    assert miss is None


@pytest.mark.asyncio
async def test_engagement_격리_기본(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-x", "e1-content")
    # 다른 engagement 로 조회 시 기본은 miss (cross 옵션 off)
    miss = await cache.get("e2", "ui_section", "hash-x")
    assert miss is None


@pytest.mark.asyncio
async def test_cross_engagement_lookup(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-x", "shared-content")
    hit = await cache.get("e2", "ui_section", "hash-x",
                           allow_cross_engagement=True)
    assert hit is not None
    assert hit["content"] == "shared-content"


@pytest.mark.asyncio
async def test_put_덮어쓰기(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-y", "v1")
    await cache.put("e1", "ui_section", "hash-y", "v2")  # 덮어씀
    hit = await cache.get("e1", "ui_section", "hash-y")
    assert hit["content"] == "v2"


@pytest.mark.asyncio
async def test_invalidate_namespace(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-1", "c1")
    await cache.put("e1", "db_schema", "hash-2", "c2")
    await cache.put("e2", "ui_section", "hash-3", "c3")
    await cache.invalidate_namespace("e1")
    # e1 항목 둘 다 사라져야 함
    assert await cache.get("e1", "ui_section", "hash-1") is None
    assert await cache.get("e1", "db_schema", "hash-2") is None
    # e2 는 남아있음
    assert await cache.get("e2", "ui_section", "hash-3") is not None


@pytest.mark.asyncio
async def test_disabled_flag_항상_miss(db):
    cache = ContentHashCache(db, enabled=False)
    await cache.put("e1", "ui_section", "hash-z", "ignored")
    # put 도 bypass, get 도 bypass
    assert await cache.get("e1", "ui_section", "hash-z") is None


@pytest.mark.asyncio
async def test_hit_count_증가(db):
    cache = ContentHashCache(db)
    await cache.put("e1", "ui_section", "hash-h", "content")
    await cache.get("e1", "ui_section", "hash-h")
    await cache.get("e1", "ui_section", "hash-h")
    stats = await cache.stats()
    assert stats["total_hits"] >= 2
