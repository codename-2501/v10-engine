"""Stage 4 CoverageVerifier 테스트 (D8 Test Suite)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine.db.adapter import SQLiteAdapter
from engine.core.state_store import AtomicStateStore
from engine.core.coverage import CoverageVerifier, CoverageReport


@pytest.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE atomic_state (
          engagement_id TEXT, node_id TEXT, item_key TEXT,
          status TEXT NOT NULL DEFAULT 'PENDING',
          retry_count INTEGER NOT NULL DEFAULT 0,
          artifact_hash TEXT, reason TEXT,
          created_at TEXT, updated_at TEXT,
          PRIMARY KEY (engagement_id, node_id, item_key)
        );
        CREATE TABLE coverage_report (
          engagement_id TEXT, node_id TEXT,
          expected_count INTEGER, produced_count INTEGER,
          missing_items TEXT, retry_attempts INTEGER DEFAULT 0,
          verified_at TEXT,
          PRIMARY KEY (engagement_id, node_id)
        );
    """)
    conn.close()
    adapter = SQLiteAdapter(tmp.name)
    yield adapter
    Path(tmp.name).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_verify_완전_커버_is_complete(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store)
    for it in ["a", "b", "c"]:
        await store.reserve("e1", "n1", it)
        await store.complete("e1", "n1", it, "h")
    r = await ver.verify("e1", "n1", ["a", "b", "c"])
    assert r.is_complete
    assert r.expected_count == 3
    assert r.produced_count == 3
    assert r.coverage_ratio == 1.0


@pytest.mark.asyncio
async def test_verify_missing_검출(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store)
    await store.reserve("e1", "n1", "a")
    await store.complete("e1", "n1", "a", "h")
    r = await ver.verify("e1", "n1", ["a", "b", "c"])
    assert r.missing == ["b", "c"]
    assert r.produced_count == 1
    assert not r.is_complete


@pytest.mark.asyncio
async def test_verify_SKIPPED_은_produced_포함(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store)
    await store.mark_skipped("e1", "n1", "b", "user skip")
    await store.reserve("e1", "n1", "a")
    await store.complete("e1", "n1", "a", "h")
    r = await ver.verify("e1", "n1", ["a", "b", "c"])
    # a=COMPLETE, b=SKIPPED → 둘 다 produced_count 2
    assert r.produced_count == 2
    assert "b" not in r.missing


@pytest.mark.asyncio
async def test_retry_missing_성공_시_COMPLETE(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store, max_retries=3)

    async def regen(item):
        return f"<section>{item}</section>"

    # item b 는 FAILED 상태
    await store.reserve("e1", "n1", "b")
    await store.fail("e1", "n1", "b", "prev err")
    remaining = await ver.retry_missing("e1", "n1", ["b"], regen)
    assert remaining == []
    assert await store.get_status("e1", "n1", "b") == "COMPLETE"


@pytest.mark.asyncio
async def test_retry_missing_max_retries_초과_NEEDS_HUMAN(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store, max_retries=2)

    async def regen(item):
        return None  # 항상 실패

    # retry_count 이미 2회 → 3번째 시도에서 NEEDS_HUMAN
    for _ in range(2):
        await store.reserve("e1", "n1", "x")
        await store.fail("e1", "n1", "x", "err")
    remaining = await ver.retry_missing("e1", "n1", ["x"], regen)
    assert "x" in remaining
    assert await store.get_status("e1", "n1", "x") == "NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_save_report_후_get_report(db):
    store = AtomicStateStore(db)
    ver = CoverageVerifier(db, store)
    await store.reserve("e1", "n1", "a")
    await store.complete("e1", "n1", "a", "h")
    r = await ver.verify("e1", "n1", ["a", "b"])
    await ver.save_report("e1", "n1", r)
    loaded = await ver.get_report("e1", "n1")
    assert loaded is not None
    assert loaded.expected_count == 2
    assert loaded.produced_count == 1
    assert "b" in loaded.missing
