"""Phase 1-2 회귀 — 페이지 레시피 ↔ 라이브러리 cross-reference."""
from __future__ import annotations

import json

import pytest

from engine.skills.qa.cross_reference import (
    CrossRefResult,
    _walk_component_names,
    build_library_extend_prompt,
    verify_component_consistency,
)


class _FakeDB:
    """fetchall stub — composition_components / composition_recipes 두 SQL 처리."""

    def __init__(self, components: list[dict], recipes: list[dict]):
        self._components = components
        self._recipes = recipes

    async def fetchall(self, sql, params=None):
        if "composition_components" in sql:
            return self._components
        if "composition_recipes" in sql:
            return self._recipes
        return []


def test_walk_component_names_단일_dict():
    obj = {"component_name": "kpi_card"}
    assert _walk_component_names(obj) == {"kpi_card"}


def test_walk_component_names_중첩_list():
    obj = {
        "pages": [
            {"components": [
                {"component_name": "page_header"},
                {"component_name": "kpi_card"},
            ]},
            {"components": [{"component_name": "page_footer"}]},
        ],
    }
    assert _walk_component_names(obj) == {"page_header", "kpi_card", "page_footer"}


@pytest.mark.asyncio
async def test_정합성_100_pass():
    db = _FakeDB(
        components=[{"name": "page_header"}, {"name": "kpi_card"}],
        recipes=[{"data": json.dumps({
            "page_name": "홈",
            "components": [
                {"component_name": "page_header"},
                {"component_name": "kpi_card"},
            ],
        })}],
    )
    r = await verify_component_consistency(db, "p1")
    assert r.severity == "pass"
    assert r.match_ratio == 1.0
    assert r.missing == set()


@pytest.mark.asyncio
async def test_부분_매칭_warn():
    db = _FakeDB(
        components=[{"name": "page_header"}, {"name": "kpi_card"}],
        recipes=[{"data": json.dumps({
            "components": [
                {"component_name": "page_header"},  # match
                {"component_name": "kpi_card"},      # match
                {"component_name": "alarm_panel"},   # missing
            ],
        })}],
    )
    r = await verify_component_consistency(
        db, "p1", fail_threshold=0.95, warn_threshold=0.6,
    )
    # 2/3 = 0.67 → warn
    assert r.severity == "warn"
    assert r.missing == {"alarm_panel"}


@pytest.mark.asyncio
async def test_심각_미달_fail():
    """이번 세션 51% 매칭 케이스 — fail level."""
    db = _FakeDB(
        components=[{"name": "page_header"}, {"name": "kpi_card"}],
        recipes=[{"data": json.dumps({
            "components": [
                {"component_name": "page_header"},  # match
                {"component_name": "alarm_panel"},   # missing
                {"component_name": "anomaly_score_card"},  # missing
                {"component_name": "threshold_gauge"},     # missing
            ],
        })}],
    )
    r = await verify_component_consistency(db, "p1")
    # 1/4 = 25% → fail
    assert r.severity == "fail"
    assert r.suggested_action == "extend_library"
    assert "alarm_panel" in r.missing


@pytest.mark.asyncio
async def test_레시피_없으면_pass():
    db = _FakeDB(components=[{"name": "x"}], recipes=[])
    r = await verify_component_consistency(db, "p1")
    assert r.severity == "pass"


@pytest.mark.asyncio
async def test_JSON_깨진_레시피_regex_fallback():
    """data 가 JSON 파싱 실패해도 regex 로 component_name 추출."""
    broken = '{"components": [{"component_name": "kpi_card"}}'  # 잘못된 brace
    db = _FakeDB(
        components=[{"name": "kpi_card"}],
        recipes=[{"data": broken}],
    )
    r = await verify_component_consistency(db, "p1")
    assert "kpi_card" in r.matched


def test_extend_prompt_포함_missing():
    prompt = build_library_extend_prompt({"alarm_panel", "kpi_card"})
    assert "alarm_panel" in prompt
    assert "kpi_card" in prompt
    assert "snake_case" in prompt


def test_extend_prompt_빈_set_빈_string():
    assert build_library_extend_prompt(set()) == ""
