"""Stage 26 PRDClarifier 테스트."""
from __future__ import annotations

import pytest
import sqlite3
import tempfile
from pathlib import Path

from engine.db.adapter import SQLiteAdapter
from engine.intake.prd_clarifier import (
    PRDClarifier, Ambiguity, Question, AMBIGUITY_PATTERNS,
)


@pytest.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE prd_clarifications (
          engagement_id TEXT, question_id TEXT,
          question TEXT, options TEXT,
          severity TEXT NOT NULL,
          category TEXT,
          answer TEXT, answered_at TEXT,
          created_at TEXT,
          PRIMARY KEY (engagement_id, question_id)
        );
    """)
    conn.close()
    yield SQLiteAdapter(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


def test_scan_quantity_vague():
    clar = PRDClarifier(None)
    prd = "서비스는 약 60~80장의 화면을 포함한다."
    import asyncio
    found = asyncio.run(clar.scan_ambiguities(prd))
    categories = {a.category for a in found}
    assert "quantity_range" in categories or "quantity_vague" in categories


def test_scan_role_undefined():
    clar = PRDClarifier(None)
    prd = "관리자가 접속하여 설정을 변경한다."
    import asyncio
    found = asyncio.run(clar.scan_ambiguities(prd))
    assert any(a.category == "role_undefined" for a in found)


def test_scan_priority_conditional():
    clar = PRDClarifier(None)
    prd = "필요하면 추가 기능을 구현한다."
    import asyncio
    found = asyncio.run(clar.scan_ambiguities(prd))
    assert any(a.category == "priority_conditional" for a in found)


def test_scan_tbd():
    clar = PRDClarifier(None)
    prd = "세부 사항은 TBD."
    import asyncio
    found = asyncio.run(clar.scan_ambiguities(prd))
    assert any(a.category == "explicit_todo" and a.severity == "blocking"
               for a in found)


def test_to_questions():
    clar = PRDClarifier(None)
    amb = [Ambiguity(
        category="role_undefined", severity="advisory",
        text="관리자", context="관리자가 접속", position=0,
    )]
    qs = clar.to_questions(amb)
    assert len(qs) == 1
    assert "관리자" in qs[0].question
    assert qs[0].options  # 역할 유형 옵션 존재


@pytest.mark.asyncio
async def test_save_and_retrieve(db):
    clar = PRDClarifier(db)
    qs = [Question(
        id="q1", category="quantity_vague", severity="blocking",
        question="수량 확정", options=[],
    )]
    saved = await clar.save_questions("e1", qs)
    assert saved == 1
    unanswered = await clar.get_unanswered("e1")
    assert len(unanswered) == 1
    assert unanswered[0]["question_id"] == "q1"


@pytest.mark.asyncio
async def test_all_blocking_answered_gate(db):
    clar = PRDClarifier(db)
    qs = [
        Question(id="q1", category="tbd", severity="blocking",
                 question="?", options=[]),
        Question(id="q2", category="advisory", severity="advisory",
                 question="?", options=[]),
    ]
    await clar.save_questions("e1", qs)
    assert not await clar.all_blocking_answered("e1")  # blocking 미답변
    await clar.save_answer("e1", "q1", "답변")
    assert await clar.all_blocking_answered("e1")  # q2 는 advisory 라 OK


def test_incorporate_answers():
    clar = PRDClarifier(None)
    import asyncio
    refined = asyncio.run(clar.incorporate_answers(
        "원본 PRD", {"q1": "답변1", "q2": "답변2"},
    ))
    assert "정제 사항" in refined
    assert "답변1" in refined and "답변2" in refined
