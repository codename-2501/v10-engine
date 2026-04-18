"""phase_contract 단위 테스트 — S3-1."""
from __future__ import annotations

import pytest

from engine.core.phase_contract import (
    PHASE_REQUIRED_ARTIFACTS,
    _next_phase,
    check_phase_contract,
)


def test_next_phase_순서():
    assert _next_phase("DEFINE") == "DESIGN"
    assert _next_phase("DESIGN") == "BUILD"
    assert _next_phase("BUILD") == "VERIFY"
    assert _next_phase("VERIFY") == "DELIVER"
    assert _next_phase("DELIVER") is None
    assert _next_phase("UNKNOWN") is None


def test_required_artifacts_phase_있음():
    for ph in ("DEFINE", "DESIGN", "BUILD", "VERIFY", "DELIVER"):
        assert ph in PHASE_REQUIRED_ARTIFACTS
        assert len(PHASE_REQUIRED_ARTIFACTS[ph]) >= 1


# ---------------------------------------------------------------------------
# check_phase_contract — db None / 노드 없음 / 통과 / 위반 시나리오
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self, sql, params=None):
        return self._rows

    async def execute(self, *args, **kwargs):
        pass

    async def fetchone(self, sql, params=None):
        return None


@pytest.mark.asyncio
async def test_db_없음_FAIL():
    r = await check_phase_contract(None, "eng-1", "DEFINE")
    assert r.passed is False


@pytest.mark.asyncio
async def test_노드_0건_FAIL():
    r = await check_phase_contract(_FakeDB([]), "eng-1", "DEFINE")
    assert r.passed is False
    assert any("0건" in v for v in r.violations)


@pytest.mark.asyncio
async def test_define_통과():
    rows = [
        {"id": "1", "task_name": "요구사항 정의서", "type": "TASK", "state": "COMPLETED"},
        {"id": "2", "task_name": "기능 백로그", "type": "TASK", "state": "COMPLETED"},
        {"id": "3", "task_name": "유스케이스", "type": "TASK", "state": "COMPLETED"},
        {"id": "4", "task_name": "화면 목록 정의서", "type": "TASK", "state": "COMPLETED"},
        {"id": "5", "task_name": "리스크 등록부", "type": "TASK", "state": "COMPLETED"},
    ]
    r = await check_phase_contract(_FakeDB(rows), "eng-1", "DEFINE")
    assert r.passed is True
    assert len(r.violations) == 0


@pytest.mark.asyncio
async def test_failed_노드_있으면_FAIL():
    rows = [
        {"id": "1", "task_name": "요구사항 정의서", "type": "TASK", "state": "COMPLETED"},
        {"id": "2", "task_name": "기능 백로그", "type": "TASK", "state": "COMPLETED"},
        {"id": "3", "task_name": "유스케이스", "type": "TASK", "state": "COMPLETED"},
        {"id": "4", "task_name": "화면 목록 정의서", "type": "TASK", "state": "FAILED"},
        {"id": "5", "task_name": "리스크 등록부", "type": "TASK", "state": "COMPLETED"},
    ]
    r = await check_phase_contract(_FakeDB(rows), "eng-1", "DEFINE")
    assert r.passed is False
    assert any("FAILED" in v or "SUSPENDED" in v for v in r.violations)


@pytest.mark.asyncio
async def test_artifact_누락_FAIL():
    rows = [
        {"id": "1", "task_name": "요구사항 정의서", "type": "TASK", "state": "COMPLETED"},
        # 나머지 필수 artifact 없음
    ]
    r = await check_phase_contract(_FakeDB(rows), "eng-1", "DEFINE")
    assert r.passed is False
    # 비율 미달 + 누락 둘 다 잡혀야 함
    assert len(r.violations) >= 2
