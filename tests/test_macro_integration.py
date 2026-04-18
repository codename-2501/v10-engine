"""
tests/test_macro_integration.py  (V10)

거시 진단 hook 통합 테스트 — 인메모리 SQLite + macro_diagnose_safe 헬퍼.

검증 시나리오:
1. 키워드 매칭 → 상위 TASK INVALID 전환
2. 길이 가드 → 30자 미만 시 0 반환
3. 동시성 가드 → 한도 초과 노드는 UPDATE 0 rows
4. 킬 스위치 → V10_UPSTREAM_REWORK_KILL=1 시 즉시 비활성
5. dry-run 모드 → INVALID 안 함, audit 만 기록
6. audit 테이블 자동 생성 (migration 미적용 시뮬레이션)
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from engine.db.adapter import create_adapter


@pytest.fixture
async def db():
    """인메모리 SQLite + 최소 nodes 테이블."""
    adapter = create_adapter("sqlite:///:memory:")
    # 최소 nodes 테이블 (실제 schema 의 핵심 컬럼만)
    await adapter.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            engagement_id TEXT,
            dag_id TEXT,
            phase TEXT,
            node_type TEXT,
            task_name TEXT,
            name TEXT,
            state TEXT,
            description TEXT,
            failure_reasons TEXT,
            upstream_rework_count INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    yield adapter


async def _insert_node(db, node_id, **kw):
    """헬퍼: 테스트용 노드 삽입."""
    defaults = {
        "engagement_id": "eng-1",
        "dag_id": "dag-1",
        "phase": "BUILD",
        "node_type": "TASK",
        "task_name": "기본",
        "name": "기본",
        "state": "COMPLETED",
        "description": None,
        "failure_reasons": None,
        "upstream_rework_count": 0,
        "updated_at": "2026-04-19T00:00:00Z",
    }
    defaults.update(kw)
    cols = ", ".join(["id"] + list(defaults.keys()))
    qs = ", ".join(["?"] * (1 + len(defaults)))
    await db.execute(
        f"INSERT INTO nodes ({cols}) VALUES ({qs})",
        (node_id, *defaults.values()),
    )


@pytest.mark.asyncio
async def test_keyword_match_invalidates_upstream(db, monkeypatch):
    """키워드 매칭 → 같은 engagement 의 상위 TASK INVALID 전환."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    monkeypatch.delenv("V10_UPSTREAM_REWORK_KILL", raising=False)

    qa_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    upstream_design_id = str(uuid.uuid4())

    await _insert_node(db, qa_id, node_type="QA", state="IN_PROGRESS")
    await _insert_node(db, task_id, name="컴포넌트", task_name="컴포넌트")
    await _insert_node(
        db, upstream_design_id, phase="DESIGN",
        name="디자인 시스템", task_name="디자인 시스템",
    )

    from engine.skills.executor_cascade import macro_diagnose_safe
    affected = await macro_diagnose_safe(
        db, qa_id, task_id,
        "QA FAIL: 디자인 토큰이 정의되지 않아 컴포넌트 색상 누락",
        model_adapter=None, source="qa_verdict",
    )
    assert affected >= 1, f"upstream INVALID 전환 실패: {affected}건"

    row = await db.fetchone(
        "SELECT state FROM nodes WHERE id=?", (upstream_design_id,),
    )
    assert row["state"] == "INVALID"


@pytest.mark.asyncio
async def test_length_guard_skips_short_messages(db, monkeypatch):
    """30자 미만 추상 메시지는 즉시 0 반환 (LLM 호출 없음)."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    monkeypatch.delenv("V10_UPSTREAM_REWORK_KILL", raising=False)

    from engine.skills.executor_cascade import macro_diagnose_safe
    result = await macro_diagnose_safe(
        db, "qa-id", "task-id", "ValueError",
        model_adapter=None, source="task_exception",
    )
    assert result == 0


@pytest.mark.asyncio
async def test_kill_switch_disables_diagnostic(db, monkeypatch):
    """V10_UPSTREAM_REWORK_KILL=1 이면 매칭 가능한 텍스트도 0 반환."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    monkeypatch.setenv("V10_UPSTREAM_REWORK_KILL", "1")

    qa_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    upstream_id = str(uuid.uuid4())
    await _insert_node(db, qa_id, node_type="QA")
    await _insert_node(db, task_id)
    await _insert_node(db, upstream_id, phase="DESIGN", task_name="디자인 시스템")

    from engine.skills.executor_cascade import macro_diagnose_safe
    result = await macro_diagnose_safe(
        db, qa_id, task_id,
        "QA FAIL: 디자인 토큰 누락으로 컴포넌트 빌드 불가",
        source="qa_verdict",
    )
    assert result == 0
    # upstream 은 그대로 COMPLETED
    row = await db.fetchone(
        "SELECT state FROM nodes WHERE id=?", (upstream_id,),
    )
    assert row["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_concurrency_limit_atomic_update(db, monkeypatch):
    """upstream_rework_count 한도 초과한 노드는 UPDATE 적용 안 됨 (atomic 가드)."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    monkeypatch.delenv("V10_UPSTREAM_REWORK_KILL", raising=False)

    qa_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    upstream_id = str(uuid.uuid4())
    # 한도 (2) 도달 — 더 이상 INVALID 안 되어야 함
    await _insert_node(db, qa_id, node_type="QA")
    await _insert_node(db, task_id)
    await _insert_node(
        db, upstream_id, phase="DESIGN", task_name="디자인 시스템",
        upstream_rework_count=2,  # 한도 도달
    )

    from engine.skills.executor_cascade import macro_diagnose_safe
    affected = await macro_diagnose_safe(
        db, qa_id, task_id,
        "QA FAIL: 디자인 시스템 누락으로 빌드 불가",
        source="qa_verdict",
    )
    # 함수 자체는 candidates 발견 → 함수 내 한도 체크에서 skip
    # upstream 은 COMPLETED 그대로
    row = await db.fetchone(
        "SELECT state, upstream_rework_count FROM nodes WHERE id=?",
        (upstream_id,),
    )
    assert row["state"] == "COMPLETED", "한도 도달 노드는 INVALID 안 되어야 함"
    assert row["upstream_rework_count"] == 2


@pytest.mark.asyncio
async def test_audit_table_auto_created(monkeypatch):
    """Migration 039 미적용 DB 에서도 audit 테이블 자동 생성."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "keyword-only")
    monkeypatch.delenv("V10_UPSTREAM_REWORK_KILL", raising=False)

    # 새 인메모리 DB — audit 테이블 없음 + nodes 만 생성
    adapter = create_adapter("sqlite:///:memory:")
    await adapter.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, engagement_id TEXT, dag_id TEXT, phase TEXT,
            node_type TEXT, task_name TEXT, name TEXT, state TEXT,
            description TEXT, failure_reasons TEXT,
            upstream_rework_count INTEGER DEFAULT 0, updated_at TEXT
        )
    """)
    qa_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    upstream_id = str(uuid.uuid4())
    await _insert_node(adapter, qa_id, node_type="QA")
    await _insert_node(adapter, task_id)
    await _insert_node(
        adapter, upstream_id, phase="DESIGN", task_name="디자인 시스템",
    )

    # _AUDIT_TABLE_ENSURED 글로벌 리셋 (테스트 격리)
    import engine.skills.executor_cascade as ec
    ec._AUDIT_TABLE_ENSURED = False

    from engine.skills.executor_cascade import macro_diagnose_safe
    await macro_diagnose_safe(
        adapter, qa_id, task_id,
        "QA FAIL: 디자인 토큰 미정의로 컴포넌트 빌드 불가",
        source="qa_verdict",
    )

    # audit 테이블 자동 생성 확인
    row = await adapter.fetchone(
        "SELECT COUNT(*) AS cnt FROM upstream_rework_audit"
    )
    assert row["cnt"] >= 1, "audit 자동 생성 + 레코드 삽입 실패"


@pytest.mark.asyncio
async def test_dry_run_mode_no_invalid(db, monkeypatch):
    """dry-run 모드 — 로그/audit 만, INVALID 전환 안 함."""
    monkeypatch.setenv("V10_UPSTREAM_REWORK_MODE", "dry-run")
    monkeypatch.delenv("V10_UPSTREAM_REWORK_KILL", raising=False)

    qa_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    upstream_id = str(uuid.uuid4())
    await _insert_node(db, qa_id, node_type="QA")
    await _insert_node(db, task_id)
    await _insert_node(db, upstream_id, phase="DESIGN", task_name="디자인 시스템")

    from engine.skills.executor_cascade import macro_diagnose_safe
    affected = await macro_diagnose_safe(
        db, qa_id, task_id,
        "QA FAIL: 디자인 토큰 누락으로 컴포넌트 빌드 불가",
        source="qa_verdict",
    )
    # dry-run → trigger_upstream_rework_if_needed 가 0 반환 → 헬퍼도 0
    assert affected == 0
    # upstream 은 COMPLETED 그대로
    row = await db.fetchone(
        "SELECT state FROM nodes WHERE id=?", (upstream_id,),
    )
    assert row["state"] == "COMPLETED"
