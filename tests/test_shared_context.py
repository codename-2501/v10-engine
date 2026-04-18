"""Stage 23 SharedContextLedger 테스트."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine.db.adapter import SQLiteAdapter
from engine.core.shared_context import SharedContextLedger


@pytest.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE shared_context (
          engagement_id TEXT, node_id TEXT, context_key TEXT,
          value TEXT, origin_item TEXT, version INTEGER DEFAULT 1,
          created_at TEXT,
          PRIMARY KEY (engagement_id, node_id, context_key)
        );
    """)
    conn.close()
    yield SQLiteAdapter(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


def test_extract_nav_footer():
    ledger = SharedContextLedger(None, enabled=False)
    html = "<nav><a href='/'>홈</a></nav><footer>©2025</footer>"
    ex = ledger._extract(html)
    assert "nav_menu" in ex
    assert "footer" in ex


def test_extract_route_map():
    ledger = SharedContextLedger(None, enabled=False)
    html = "<a href='/home'>a</a><a href='/about'>b</a><a href='https://ex.com'>c</a>"
    ex = ledger._extract(html)
    import json
    routes = json.loads(ex["route_map"])
    assert "/home" in routes
    assert "/about" in routes
    # 외부 링크 제외
    assert "https://ex.com" not in routes


def test_extract_common_actions():
    ledger = SharedContextLedger(None, enabled=False)
    html = "<button>저장</button><button>취소</button><button>저장</button>"
    ex = ledger._extract(html)
    import json
    actions = json.loads(ex["common_actions"])
    assert "저장" in actions
    assert "취소" in actions
    # 중복 제거
    assert actions.count("저장") == 1


@pytest.mark.asyncio
async def test_record_and_snippet(db):
    ledger = SharedContextLedger(db)
    html = "<nav>M</nav><footer>F</footer><a href='/x'>X</a><button>go</button>"
    new = await ledger.extract_and_record("e1", "n1", html, "item-a")
    assert new >= 2
    snippet = await ledger.as_prompt_snippet("e1", "n1")
    assert "기존 결정사항" in snippet
    assert "nav_menu" in snippet


@pytest.mark.asyncio
async def test_first_wins(db):
    ledger = SharedContextLedger(db)
    html1 = "<nav>FIRST</nav>"
    html2 = "<nav>SECOND</nav>"
    await ledger.extract_and_record("e1", "n1", html1, "item-a")
    new2 = await ledger.extract_and_record("e1", "n1", html2, "item-b")
    # 두 번째 호출은 nav_menu 이미 있으니 skip
    snippet = await ledger.as_prompt_snippet("e1", "n1")
    assert "FIRST" in snippet
    assert "SECOND" not in snippet


@pytest.mark.asyncio
async def test_invalidate(db):
    ledger = SharedContextLedger(db)
    await ledger.extract_and_record("e1", "n1", "<nav>n</nav>", "a")
    await ledger.invalidate("e1", "n1")
    snippet = await ledger.as_prompt_snippet("e1", "n1")
    assert snippet == ""


@pytest.mark.asyncio
async def test_disabled_flag(db):
    ledger = SharedContextLedger(db, enabled=False)
    new = await ledger.extract_and_record("e1", "n1", "<nav>n</nav>", "a")
    assert new == 0
