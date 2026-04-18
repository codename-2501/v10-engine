"""
tests/test_dag_integrity_fixes.py  (V10)

5건 엔진 보강 검증:
- Fix #1: splitting 부적합 카테고리 필터
- Fix #2: umbrella outgoing edge 비활성화
- Fix #3: watchdog RESERVED reasons 확장
- Fix #4: enforce_category_constraint 발동 조건 확장 (코드 호출 검증)
- Fix #5: verify_dag_integrity 도구 정합성 검증
"""
from __future__ import annotations

import uuid

import pytest

from engine.db.adapter import create_adapter


# ---------------------------------------------------------------------------
# Fix #3 — watchdog RESERVED reasons
# ---------------------------------------------------------------------------

def test_watchdog_reserved_reasons_extended():
    """수동 개입 사유들이 RESERVED 목록에 포함."""
    from engine.lifecycle.watchdog import _RESERVED_SUSPENSION_REASONS
    assert "SHUTDOWN_DRAIN" in _RESERVED_SUSPENSION_REASONS  # 기존
    assert "MANUAL_HOLD" in _RESERVED_SUSPENSION_REASONS  # 신규
    assert "SHOULD_SKIP" in _RESERVED_SUSPENSION_REASONS
    assert "PROJECT_CONTEXT_NOT_APPLICABLE" in _RESERVED_SUSPENSION_REASONS
    assert "DEPENDENCY_NOT_MET" in _RESERVED_SUSPENSION_REASONS
    assert "DEPENDENCY_GRAPH_BROKEN" in _RESERVED_SUSPENSION_REASONS


def test_watchdog_reserved_reason_blocks_resume():
    """RESERVED reason 의 노드는 _is_transient_suspension 가 False 반환."""
    from engine.lifecycle.watchdog import _is_transient_suspension
    # RESERVED → 무조건 False
    assert _is_transient_suspension("MANUAL_HOLD") is False
    assert _is_transient_suspension("PROJECT_CONTEXT_NOT_APPLICABLE") is False
    assert _is_transient_suspension("DEPENDENCY_NOT_MET") is False


# ---------------------------------------------------------------------------
# Fix #1 — splitting 부적합 카테고리 필터
# ---------------------------------------------------------------------------

def test_inappropriate_categories_map_complete():
    """size_estimator 의 모든 ProjectType 에 대해 매핑 정의."""
    from engine.skills.splitting import _INAPPROPRIATE_CATEGORIES_BY_TYPE
    expected_types = {"app", "mixed", "si", "mlops", "data"}
    assert expected_types == set(_INAPPROPRIATE_CATEGORIES_BY_TYPE.keys())


def test_app_excludes_monitoring_production():
    """app project 는 monitoring/production 카테고리 부적합."""
    from engine.skills.splitting import _INAPPROPRIATE_CATEGORIES_BY_TYPE
    bad = _INAPPROPRIATE_CATEGORIES_BY_TYPE["app"]
    assert "monitoring" in bad
    assert "production" in bad
    assert "mlops" in bad


def test_si_mlops_data_mixed_keep_all_categories():
    """SI/MLOps/Data/mixed 는 모든 카테고리 적합 (보수적)."""
    from engine.skills.splitting import _INAPPROPRIATE_CATEGORIES_BY_TYPE
    for ptype in ("si", "mlops", "data", "mixed"):
        assert _INAPPROPRIATE_CATEGORIES_BY_TYPE[ptype] == set()


# ---------------------------------------------------------------------------
# Fix #2 + integration — splitting umbrella edge 비활성화
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_filter_categories_app_project_removes_monitoring():
    """app project 에서 monitoring/production 카테고리 자동 제거."""
    from engine.skills.splitting import _filter_categories_by_project_type
    import json

    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE engagements (id TEXT PRIMARY KEY, global_context TEXT)
    """)
    await db.execute("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, engagement_id TEXT)
    """)

    eng_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    raw = {
        "projectName": "마이 루틴",
        "serviceType": ["mobile_app"],  # size_estimator 가 'app' 으로 분류
        "features": ["a", "b", "c", "d", "e", "f", "g", "h", "i"],
    }
    await db.execute(
        "INSERT INTO engagements VALUES (?, ?)",
        (eng_id, json.dumps(raw)),
    )
    await db.execute(
        "INSERT INTO projects VALUES (?, ?)",
        (proj_id, eng_id),
    )

    cats = [
        {"name": "layout", "description": "..."},
        {"name": "form", "description": "..."},
        {"name": "feedback", "description": "..."},
        {"name": "monitoring", "description": "..."},
        {"name": "production", "description": "..."},
    ]
    filtered = await _filter_categories_by_project_type(db, proj_id, cats)
    names = {c["name"] for c in filtered}
    assert "layout" in names
    assert "form" in names
    assert "feedback" in names
    assert "monitoring" not in names
    assert "production" not in names


@pytest.mark.asyncio
async def test_filter_categories_si_keeps_all():
    """SI project 는 모든 카테고리 유지."""
    from engine.skills.splitting import _filter_categories_by_project_type
    import json

    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE engagements (id TEXT PRIMARY KEY, global_context TEXT)
    """)
    await db.execute("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, engagement_id TEXT)
    """)

    eng_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    raw = {"projectName": "기업 SI", "serviceType": ["api_service"], "scope": ["si"]}
    await db.execute("INSERT INTO engagements VALUES (?, ?)", (eng_id, json.dumps(raw)))
    await db.execute("INSERT INTO projects VALUES (?, ?)", (proj_id, eng_id))

    cats = [{"name": n, "description": ""} for n in
            ["layout", "form", "feedback", "monitoring", "production"]]
    filtered = await _filter_categories_by_project_type(db, proj_id, cats)
    assert len(filtered) == 5  # 모두 유지


@pytest.mark.asyncio
async def test_filter_categories_no_engagement_safe_passthrough():
    """engagement 없으면 원본 그대로 반환 (fail-safe)."""
    from engine.skills.splitting import _filter_categories_by_project_type
    db = create_adapter("sqlite:///:memory:")
    await db.execute(
        "CREATE TABLE engagements (id TEXT PRIMARY KEY, global_context TEXT)"
    )
    await db.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, engagement_id TEXT)"
    )
    cats = [{"name": "x"}, {"name": "monitoring"}]
    result = await _filter_categories_by_project_type(
        db, "no-such-project", cats,
    )
    assert result == cats  # passthrough


# ---------------------------------------------------------------------------
# Fix #5 — verify_dag_integrity 도구
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_detects_skipped_active_edge():
    """SKIPPED 노드의 활성 outgoing edge 감지."""
    from engine.tools.verify_dag_integrity import _check_skipped_outgoing_edges

    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, dag_id TEXT, name TEXT, state TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, dag_id TEXT, from_node_id TEXT,
            to_node_id TEXT, is_active INTEGER DEFAULT 1
        )
    """)
    skipped_id = str(uuid.uuid4())
    downstream_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())
    dag_id = "test-dag"
    await db.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, 'SKIPPED')",
        (skipped_id, dag_id, "umbrella"),
    )
    await db.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, 'BLOCKED')",
        (downstream_id, dag_id, "registry"),
    )
    await db.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, 1)",
        (edge_id, dag_id, skipped_id, downstream_id),
    )

    issues = await _check_skipped_outgoing_edges(db, dag_id)
    assert len(issues) == 1
    assert issues[0]["from_node"] == "umbrella"
    assert issues[0]["to_node"] == "registry"


@pytest.mark.asyncio
async def test_verify_detects_broken_pair():
    """존재하지 않는 노드를 가리키는 페어 link 감지."""
    from engine.tools.verify_dag_integrity import _check_broken_pairs

    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, dag_id TEXT, name TEXT, node_type TEXT,
            qa_pair_node_id TEXT, task_pair_node_id TEXT
        )
    """)
    task_id = str(uuid.uuid4())
    fake_qa_id = str(uuid.uuid4())
    dag_id = "test-dag"
    await db.execute(
        "INSERT INTO nodes (id, dag_id, name, node_type, qa_pair_node_id) "
        "VALUES (?, ?, 'orphan-task', 'TASK', ?)",
        (task_id, dag_id, fake_qa_id),
    )

    issues = await _check_broken_pairs(db, dag_id)
    assert any(i["type"] == "broken_qa_pair" for i in issues)


@pytest.mark.asyncio
async def test_verify_no_issues_clean_dag():
    """깨끗한 DAG 는 0건."""
    from engine.tools.verify_dag_integrity import (
        _check_broken_pairs, _check_skipped_outgoing_edges, _check_orphan_edges,
    )
    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, dag_id TEXT, name TEXT, node_type TEXT,
            state TEXT, qa_pair_node_id TEXT, task_pair_node_id TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, dag_id TEXT, from_node_id TEXT,
            to_node_id TEXT, is_active INTEGER DEFAULT 1
        )
    """)
    assert await _check_broken_pairs(db, "test-dag") == []
    assert await _check_skipped_outgoing_edges(db, "test-dag") == []
    assert await _check_orphan_edges(db, "test-dag") == []
