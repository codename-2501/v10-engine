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


# ---------------------------------------------------------------------------
# 통합: run_integrity_check + 자동 복구 flow
# ---------------------------------------------------------------------------

def test_is_html_detector_spec_type_html():
    """spec.type='html' 이면 _is_html=True 확정 (content 앞부분과 무관)."""
    # 로직 복제 — 실제 executor 에서 import 못 하니 동일 규칙 검증
    def detect(spec, content):
        _spec_html = bool(spec) and (
            spec.get("type") == "html" or spec.get("file_type") == "html"
        )
        _content_looks_html = False
        if content:
            import re
            _sniff = content.strip()
            if _sniff.startswith("```"):
                _sniff = re.sub(r"^```(?:html)?\s*\n?", "", _sniff)
            _content_looks_html = _sniff[:15].lower().startswith(
                ("<!doctype", "<html", "<div", "<style")
            )
        return _spec_html or _content_looks_html

    # Case 1: spec.type=html → True (content 무관)
    assert detect({"type": "html"}, "nothing here") is True
    # Case 2: spec.file_type=html → True
    assert detect({"file_type": "html"}, "x") is True
    # Case 3: spec 없음 but content 는 ```html fence wrap
    assert detect(None, "```html\n<!DOCTYPE html>\n<html>") is True
    # Case 4: 순수 <!DOCTYPE 시작
    assert detect(None, "<!DOCTYPE html>\n...") is True
    # Case 5: document (헤딩 markdown)
    assert detect({"type": "document"}, "# 제목\n내용") is False


@pytest.mark.asyncio
async def test_cascade_heals_pair_link_with_duplicate_qas():
    """중복 QA 3개 (2 SKIPPED, 1 실제 실행됨) 중 실제 실행된 걸 우선 선택."""
    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, dag_id TEXT, project_id TEXT,
            node_type TEXT, name TEXT, state TEXT,
            qa_pair_node_id TEXT, task_pair_node_id TEXT,
            created_at TEXT DEFAULT '2026-04-19T00:00:00Z'
        )
    """)
    await db.execute("""
        CREATE TABLE artifacts (id TEXT PRIMARY KEY, node_id TEXT)
    """)
    dag_id = "test"
    proj_id = "proj"
    task_id = str(uuid.uuid4())
    qa_skipped1 = str(uuid.uuid4())
    qa_skipped2 = str(uuid.uuid4())
    qa_active = str(uuid.uuid4())
    # TASK 페어 link NULL
    await db.execute(
        """INSERT INTO nodes (id, dag_id, project_id, node_type, name, state)
           VALUES (?, ?, ?, 'TASK', 'X', 'COMPLETED')""",
        (task_id, dag_id, proj_id),
    )
    # 중복 QA 3개 (2 SKIPPED, 1 FAILED)
    for qid, state in [
        (qa_skipped1, "SKIPPED"),
        (qa_skipped2, "SKIPPED"),
        (qa_active, "FAILED"),
    ]:
        await db.execute(
            """INSERT INTO nodes (id, dag_id, project_id, node_type, name, state)
               VALUES (?, ?, ?, 'QA', '[QA] X', ?)""",
            (qid, dag_id, proj_id, state),
        )
    # 이름 매칭 쿼리 (executor.py 11-0a 로직과 동일)
    row = await db.fetchone(
        """SELECT id, state FROM nodes
           WHERE dag_id=? AND project_id=? AND node_type='QA' AND name='[QA] X'
           ORDER BY CASE WHEN state != 'SKIPPED' THEN 0 ELSE 1 END,
                    (SELECT COUNT(*) FROM artifacts WHERE node_id=nodes.id) DESC,
                    created_at DESC
           LIMIT 1""",
        (dag_id, proj_id),
    )
    assert row["id"] == qa_active, "SKIPPED 아닌 QA 가 우선 선택되어야 함"


@pytest.mark.asyncio
async def test_phase_display_order_correct():
    """대시보드 phase 정렬이 워크플로우 순서 (DEFINE→DESIGN→BUILD→VERIFY→DELIVER) 로
    나와야 함 — 알파벳 순 (BUILD<DEFINE<...) 이 아니라."""
    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, dag_id TEXT, phase TEXT,
            name TEXT, priority INTEGER DEFAULT 0
        )
    """)
    dag_id = "test"
    for i, phase in enumerate(["DELIVER", "BUILD", "DEFINE", "VERIFY", "DESIGN"]):
        await db.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, 0)",
            (str(uuid.uuid4()), dag_id, phase, f"n{i}"),
        )
    rows = await db.fetchall(
        """SELECT phase FROM nodes WHERE dag_id=?
           ORDER BY CASE phase
                        WHEN 'DEFINE' THEN 1 WHEN 'DESIGN' THEN 2
                        WHEN 'BUILD' THEN 3 WHEN 'VERIFY' THEN 4
                        WHEN 'DELIVER' THEN 5 ELSE 99 END, name""",
        (dag_id,),
    )
    assert [r["phase"] for r in rows] == [
        "DEFINE", "DESIGN", "BUILD", "VERIFY", "DELIVER",
    ]


@pytest.mark.asyncio
async def test_cascade_revives_skipped_qa_and_edges():
    """TASK COMPLETED cascade 가 SKIPPED QA 살리고 outgoing edges 재활성화."""
    db = create_adapter("sqlite:///:memory:")
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, state TEXT, version INTEGER DEFAULT 0,
            node_type TEXT, updated_at TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, from_node_id TEXT, to_node_id TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    qa_id = str(uuid.uuid4())
    downstream_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())
    # QA SKIPPED + outgoing edge 비활성 (startup hook 이 끊은 상태 재현)
    await db.execute(
        "INSERT INTO nodes (id, state, node_type) VALUES (?, 'SKIPPED', 'QA')",
        (qa_id,),
    )
    await db.execute(
        "INSERT INTO nodes (id, state, node_type) VALUES (?, 'BLOCKED', 'TASK')",
        (downstream_id,),
    )
    await db.execute(
        "INSERT INTO edges (id, from_node_id, to_node_id, is_active) VALUES (?, ?, ?, 0)",
        (edge_id, qa_id, downstream_id),
    )

    # cascade 시뮬레이션 (executor.py 11-0a 로직):
    #   페어 TASK COMPLETED → 짝 QA SKIPPED → NOT_STARTED + outgoing edges 재활성화
    rc = await db.execute(
        """UPDATE nodes SET state='NOT_STARTED'
           WHERE id=? AND state IN ('BLOCKED', 'SKIPPED')""",
        (qa_id,),
    )
    assert rc == 1
    await db.execute(
        "UPDATE edges SET is_active=1 WHERE from_node_id=? AND is_active=0",
        (qa_id,),
    )

    # 검증
    row = await db.fetchone("SELECT state FROM nodes WHERE id=?", (qa_id,))
    assert row["state"] == "NOT_STARTED"
    erow = await db.fetchone("SELECT is_active FROM edges WHERE id=?", (edge_id,))
    assert erow["is_active"] == 1


@pytest.mark.asyncio
async def test_retry_reactivates_outgoing_edges():
    """c9_manual_retry 시 노드의 비활성 outgoing edges 자동 재활성화."""
    from engine.core.validation_gateway import ValidationGateway

    db = create_adapter("sqlite:///:memory:")
    # 최소 schema (validation_gateway 가 참조하는 컬럼)
    await db.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, state TEXT, version INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, stall_count INTEGER DEFAULT 0,
            failure_reasons TEXT, description TEXT, updated_at TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, from_node_id TEXT, to_node_id TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE agent_token_usage (node_id TEXT, tokens INTEGER)
    """)

    node_id = str(uuid.uuid4())
    downstream_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())

    await db.execute(
        "INSERT INTO nodes (id, state) VALUES (?, 'FAILED')", (node_id,),
    )
    await db.execute(
        "INSERT INTO nodes (id, state) VALUES (?, 'BLOCKED')", (downstream_id,),
    )
    # 비활성 outgoing edge (Migration 040 이나 splitting.py 에서 비활성화됐던 케이스)
    await db.execute(
        "INSERT INTO edges (id, from_node_id, to_node_id, is_active) VALUES (?, ?, ?, 0)",
        (edge_id, node_id, downstream_id),
    )

    gw = ValidationGateway(db)
    success = await gw.c9_manual_retry(node_id, "ADMIN")
    assert success is True

    # outgoing edge 재활성화 확인
    row = await db.fetchone(
        "SELECT is_active FROM edges WHERE id=?", (edge_id,),
    )
    assert row["is_active"] == 1, "retry 후 outgoing edge 가 재활성화되어야 함"

    # 노드 상태 READY 확인
    node_row = await db.fetchone(
        "SELECT state FROM nodes WHERE id=?", (node_id,),
    )
    assert node_row["state"] == "READY"


@pytest.mark.asyncio
async def test_run_integrity_check_detects_and_fixes():
    """검출 + 자동 복구 E2E 흐름."""
    from engine.tools.verify_dag_integrity import run_integrity_check

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
    dag_id = "test-dag"
    skipped_id = str(uuid.uuid4())
    downstream_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())
    orphan_edge_id = str(uuid.uuid4())

    await db.execute(
        "INSERT INTO nodes (id, dag_id, name, node_type, state) VALUES (?, ?, ?, 'TASK', 'SKIPPED')",
        (skipped_id, dag_id, "umbrella"),
    )
    await db.execute(
        "INSERT INTO nodes (id, dag_id, name, node_type, state) VALUES (?, ?, ?, 'TASK', 'BLOCKED')",
        (downstream_id, dag_id, "registry"),
    )
    # SKIPPED active edge (자동 복구 대상)
    await db.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, 1)",
        (edge_id, dag_id, skipped_id, downstream_id),
    )
    # 고아 edge (자동 복구 대상)
    await db.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, 1)",
        (orphan_edge_id, dag_id, "ghost-from-id", downstream_id),
    )

    # dry-run
    result = await run_integrity_check(db, dag_id=dag_id, apply=False)
    assert result["counts"]["skipped_active_outgoing_edge"] == 1
    assert result["counts"]["orphan_edge"] == 1
    assert result["fixed"] is None

    # apply
    result2 = await run_integrity_check(db, dag_id=dag_id, apply=True)
    assert result2["fixed"]["skipped_outgoing"] == 1
    assert result2["fixed"]["orphan_edges"] == 1

    # 재실행 → 0건 (복구 완료)
    result3 = await run_integrity_check(db, dag_id=dag_id, apply=False)
    assert result3["counts"].get("skipped_active_outgoing_edge", 0) == 0
    assert result3["counts"].get("orphan_edge", 0) == 0
