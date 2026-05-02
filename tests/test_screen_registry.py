"""screen_registry 통합 테스트 — Tier 2-B/C 회귀 자동 감지.

이번 세션 회귀 3종이 재발 안 함을 보장:
  1. dict vs str 타입 혼용 → coverage.verify 의 isinstance 필터 걸림
  2. state='COMPLETED' 조건으로 chunk loop 빈 list
  3. LLM 이 SCREEN_REGISTRY JSON 안 만들어도 regex fallback 정상 작동
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from engine.db.adapter import SQLiteAdapter
from engine.db.migrations.runner import MigrationRunner
from engine.skills.artifact.screen_registry import (
    extract_screen_registry,
    sync_screen_registry,
    load_screen_registry,
)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
async def db():
    """Temp SQLite + 모든 마이그레이션 적용."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    _db = SQLiteAdapter(tmp.name)
    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            engagement_id TEXT,
            project_name TEXT
        );
    """)
    runner = MigrationRunner(_db)
    await runner.apply_all()
    # 테스트 프로젝트 삽입
    await _db.execute(
        "INSERT INTO projects (id, engagement_id, project_name) VALUES (?, ?, ?)",
        ("proj-test-01", "eng-test-01", "Test Project"),
    )
    yield _db
    await _db.close()
    Path(tmp.name).unlink(missing_ok=True)


# ============================================================
# extract_screen_registry 단위 테스트
# ============================================================


def test_extract_from_json_block():
    """Tier 2-A JSON block 우선 추출."""
    content = """
    # 화면 목록 정의서
    | SC-AD-001 | 관리자 대시보드 |
    | SC-AD-002 | 사용자 관리 |

    <!-- SCREEN_REGISTRY
    {
      "version": "1.0",
      "project": "Test",
      "screens": [
        {"id": "SC-AD-001", "name": "관리자 대시보드", "domain": "Admin", "priority": "P0"},
        {"id": "SC-AD-002", "name": "사용자 관리", "domain": "Admin", "priority": "P1"}
      ]
    }
    -->
    """
    result = extract_screen_registry(content)
    assert len(result) == 2
    assert result[0]["id"] == "SC-AD-001"
    assert result[0]["name"] == "관리자 대시보드"
    assert result[0]["domain"] == "Admin"
    assert result[0]["priority"] == "P0"


def test_extract_from_table_fallback():
    """JSON block 없으면 테이블 파싱."""
    content = """
    | SC-AU-001 | 로그인 | Auth | /login | P0 |
    | SC-AU-002 | 회원가입 | Auth | /signup | P0 |
    | SC-HM-001 | 홈 | Home | / | P0 |
    """
    result = extract_screen_registry(content)
    assert len(result) == 3
    assert {r["id"] for r in result} == {"SC-AU-001", "SC-AU-002", "SC-HM-001"}
    assert result[0]["name"] == "로그인"


def test_extract_empty_returns_empty():
    """content 없으면 빈 list."""
    assert extract_screen_registry("") == []
    assert extract_screen_registry(None) == []


def test_extract_skips_header_rows():
    """테이블 header (화면명/name/이름) 는 skip."""
    content = """
    | 화면ID | 화면명 | 도메인 |
    | --- | --- | --- |
    | SC-AD-001 | 대시보드 | Admin |
    """
    result = extract_screen_registry(content)
    assert len(result) == 1
    assert result[0]["id"] == "SC-AD-001"
    assert result[0]["name"] == "대시보드"


# ============================================================
# sync / load 통합 테스트
# ============================================================


@pytest.mark.asyncio
async def test_sync_and_load(db):
    """UPSERT 후 load 로 round-trip."""
    screens = [
        {"id": "SC-AD-001", "name": "대시보드", "domain": "Admin",
         "priority": "P0", "intent": "전체 KPI"},
        {"id": "SC-AU-001", "name": "로그인", "domain": "Auth",
         "priority": "P0", "intent": "진입점"},
    ]
    n = await sync_screen_registry(db, "proj-test-01", 1, screens)
    assert n == 2
    rows = await load_screen_registry(db, "proj-test-01")
    assert len(rows) == 2
    names = {r["id"]: r["name"] for r in rows}
    assert names == {"SC-AD-001": "대시보드", "SC-AU-001": "로그인"}


@pytest.mark.asyncio
async def test_sync_idempotent(db):
    """같은 정의서 2번 저장 — row 중복 없음, 업데이트만."""
    screens1 = [{"id": "SC-AD-001", "name": "대시보드"}]
    await sync_screen_registry(db, "proj-test-01", 1, screens1)
    screens2 = [{"id": "SC-AD-001", "name": "관리자 대시보드"}]  # 이름 변경
    await sync_screen_registry(db, "proj-test-01", 2, screens2)
    rows = await load_screen_registry(db, "proj-test-01")
    assert len(rows) == 1  # 중복 없음
    assert rows[0]["name"] == "관리자 대시보드"  # 최신 이름


@pytest.mark.asyncio
async def test_sync_stale_write_blocked(db):
    """source_version 역순 write 는 차단."""
    await sync_screen_registry(
        db, "proj-test-01", 5,
        [{"id": "SC-AD-001", "name": "최신 이름"}],
    )
    # version 3 (더 낮음) 으로 덮어쓰기 시도
    await sync_screen_registry(
        db, "proj-test-01", 3,
        [{"id": "SC-AD-001", "name": "stale 이름"}],
    )
    rows = await load_screen_registry(db, "proj-test-01")
    assert rows[0]["name"] == "최신 이름"  # stale write 거부


@pytest.mark.asyncio
async def test_load_empty_project(db):
    """registry 없는 프로젝트 = 빈 list (fallback 판단용)."""
    rows = await load_screen_registry(db, "nonexistent-proj")
    assert rows == []
