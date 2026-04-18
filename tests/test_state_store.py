"""Stage 3 StateStore 단위/통합 테스트 (D8 Test Suite)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine.db.adapter import SQLiteAdapter
from engine.core.state_store import AtomicStateStore


@pytest.fixture
async def db():
    """임시 DB + atomic_state 테이블."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    # 직접 테이블 생성 (migration 없이 테스트용)
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE atomic_state (
          engagement_id TEXT, node_id TEXT, item_key TEXT,
          status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','RESERVED','COMPLETE','FAILED','NEEDS_HUMAN','SKIPPED')),
          retry_count INTEGER NOT NULL DEFAULT 0,
          artifact_hash TEXT, reason TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (engagement_id, node_id, item_key)
        );
    """)
    conn.close()
    adapter = SQLiteAdapter(tmp.name)
    yield adapter
    Path(tmp.name).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_reserve_신규_item_True(db):
    store = AtomicStateStore(db)
    ok = await store.reserve("e1", "n1", "item1")
    assert ok is True


@pytest.mark.asyncio
async def test_reserve_이미_COMPLETE_False(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "item1")
    await store.complete("e1", "n1", "item1", "hash-a")
    # COMPLETE 상태면 reserve False
    assert await store.reserve("e1", "n1", "item1") is False


@pytest.mark.asyncio
async def test_reserve_FAILED_재시도_허용(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "item1")
    await store.fail("e1", "n1", "item1", "oops")
    # FAILED → 다시 reserve 가능 (재시도)
    assert await store.reserve("e1", "n1", "item1") is True


@pytest.mark.asyncio
async def test_complete_기록(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "item1")
    await store.complete("e1", "n1", "item1", "hash-b")
    status = await store.get_status("e1", "n1", "item1")
    assert status == "COMPLETE"


@pytest.mark.asyncio
async def test_fail_retry_count_증가(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "item1")
    await store.fail("e1", "n1", "item1", "err1")
    await store.reserve("e1", "n1", "item1")
    await store.fail("e1", "n1", "item1", "err2")
    rc = await store.get_retry_count("e1", "n1", "item1")
    assert rc == 2


@pytest.mark.asyncio
async def test_list_incomplete_missing_추출(db):
    store = AtomicStateStore(db)
    # item1 COMPLETE, item2 FAILED, item3 없음
    await store.reserve("e1", "n1", "item1")
    await store.complete("e1", "n1", "item1", "h")
    await store.reserve("e1", "n1", "item2")
    await store.fail("e1", "n1", "item2", "err")
    expected = ["item1", "item2", "item3"]
    missing = await store.list_incomplete("e1", "n1", expected)
    # item1 은 COMPLETE 아니므로 제외. item2 FAILED, item3 없음 → 2개
    assert set(missing) == {"item2", "item3"}


@pytest.mark.asyncio
async def test_mark_skipped_incomplete_에서_제외(db):
    store = AtomicStateStore(db)
    await store.mark_skipped("e1", "n1", "item1", "user decision")
    expected = ["item1", "item2"]
    missing = await store.list_incomplete("e1", "n1", expected)
    # SKIPPED 은 incomplete 아님
    assert "item1" not in missing
    assert "item2" in missing


@pytest.mark.asyncio
async def test_mark_needs_human(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "item1")
    await store.mark_needs_human("e1", "n1", "item1", "auto-retry exhausted")
    status = await store.get_status("e1", "n1", "item1")
    assert status == "NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_summary_status별_카운트(db):
    store = AtomicStateStore(db)
    await store.reserve("e1", "n1", "a")
    await store.complete("e1", "n1", "a", "h")
    await store.reserve("e1", "n1", "b")
    await store.fail("e1", "n1", "b", "err")
    await store.reserve("e1", "n1", "c")  # RESERVED 유지
    summary = await store.summary("e1", "n1")
    assert summary.get("COMPLETE") == 1
    assert summary.get("FAILED") == 1
    assert summary.get("RESERVED") == 1
