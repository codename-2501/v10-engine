"""Phase 1-1 회귀 — component_registry.json schema 검증."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.skills.qa.schema_validator import (
    load_schema,
    validate_against_schema,
)


def test_schema_파일_로딩():
    schema = load_schema("schemas/component_registry.json")
    assert schema is not None
    assert schema.get("type") == "object"
    assert "pages" in schema.get("required", [])


def test_정상_레지스트리_PASS():
    valid = {
        "pages": [
            {
                "page_name": "홈",
                "page_slug": "home",
                "screen_ids": ["SC-AI-001"],
                "components": [
                    {"component_name": "page_header", "order": 1},
                    {"component_name": "kpi_card", "order": 2},
                ],
            },
        ],
    }
    r = validate_against_schema(json.dumps(valid), "schemas/component_registry.json")
    assert r.pass_, f"errors: {r.errors}"


def test_화면_ID_가_component_name_으로_들어가면_FAIL():
    """이번 세션 발견 broken 패턴 — name 필드에 SC-XXX 들어감."""
    invalid = {
        "pages": [
            {
                "page_name": "홈",
                "page_slug": "home",
                "components": [
                    {"component_name": "SC-HB-006", "order": 1},  # invalid pattern
                ],
            },
        ],
    }
    r = validate_against_schema(
        json.dumps(invalid), "schemas/component_registry.json"
    )
    assert r.pass_ is False
    assert any("component_name" in e or "SC-HB" in e or "pattern" in e.lower()
               for e in r.errors)


def test_빈_pages_FAIL():
    invalid = {"pages": []}
    r = validate_against_schema(
        json.dumps(invalid), "schemas/component_registry.json"
    )
    assert r.pass_ is False


def test_page_slug_대문자_FAIL():
    invalid = {
        "pages": [
            {
                "page_name": "Home",
                "page_slug": "Home-Page",  # 대문자 invalid
                "components": [{"component_name": "page_header"}],
            },
        ],
    }
    r = validate_against_schema(
        json.dumps(invalid), "schemas/component_registry.json"
    )
    assert r.pass_ is False


def test_JSON_파싱_실패_FAIL():
    r = validate_against_schema("not json", "schemas/component_registry.json")
    assert r.pass_ is False
    assert any("JSON" in e for e in r.errors)


def test_schema_없으면_pass_graceful():
    """존재하지 않는 schema_ref → graceful pass (log only)."""
    r = validate_against_schema('{"x":1}', "schemas/nonexistent.json")
    assert r.pass_ is True
