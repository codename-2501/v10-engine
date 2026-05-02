"""W1 회귀 — dual output 마커 분리 + schema 검증 + fallback."""
from __future__ import annotations

import json

import pytest

from engine.skills.qa.dual_output import (
    DualOutputResult,
    is_dual_output_spec,
    load_output_formats,
    parse_and_validate_dual,
    split_by_marker,
    dual_output_directive,
)


# ============================================================
# load_output_formats — yaml 로드
# ============================================================


def test_output_formats_yaml_load_핵심_type():
    data = load_output_formats()
    types = data.get("types", {})
    assert "markdown" not in types  # markdown 은 document
    assert "document" in types
    assert "instantdb_schema" in types
    assert "openapi_yaml" in types
    assert "iam_policy_json" in types


def test_각_type_file_ext_parser_있음():
    data = load_output_formats()
    for t, cfg in data.get("types", {}).items():
        assert "file_ext" in cfg, f"{t} no file_ext"
        assert "parser" in cfg, f"{t} no parser"


# ============================================================
# split_by_marker
# ============================================================


def test_split_2개_format_정상():
    raw = """---FORMAT:markdown---
# 인간 검토용
내용 1

---FORMAT:openapi_yaml---
openapi: 3.1.0
info:
  title: API"""
    parts = split_by_marker(raw)
    assert len(parts) == 2
    assert parts[0][0] == "markdown"
    assert "인간 검토용" in parts[0][1]
    assert parts[1][0] == "openapi_yaml"
    assert "openapi: 3.1.0" in parts[1][1]


def test_split_마커_없으면_빈리스트():
    raw = "그냥 일반 텍스트 — 마커 없음"
    assert split_by_marker(raw) == []


def test_split_3개_format():
    raw = """---FORMAT:markdown---
A
---FORMAT:json_spec---
B
---FORMAT:sql_ddl---
C"""
    parts = split_by_marker(raw)
    assert [p[0] for p in parts] == ["markdown", "json_spec", "sql_ddl"]
    assert parts[0][1].strip() == "A"
    assert parts[2][1].strip() == "C"


def test_split_마커_앞뒤_공백_허용():
    raw = "  ---FORMAT:markdown---  \nbody1\n  ---FORMAT:json_spec---\nbody2"
    parts = split_by_marker(raw)
    assert [p[0] for p in parts] == ["markdown", "json_spec"]


def test_split_같은_format_중복_허용():
    """같은 format 중복 마커 → 둘 다 남음 (호출자가 결정)."""
    raw = "---FORMAT:markdown---\nA\n---FORMAT:markdown---\nB"
    parts = split_by_marker(raw)
    assert len(parts) == 2


# ============================================================
# is_dual_output_spec
# ============================================================


def test_is_dual_outputs_2개_이상():
    spec = {"outputs": [
        {"format": "markdown"},
        {"format": "openapi_yaml"},
    ]}
    assert is_dual_output_spec(spec) is True


def test_is_dual_outputs_1개_안됨():
    spec = {"outputs": [{"format": "markdown"}]}
    assert is_dual_output_spec(spec) is False


def test_is_dual_outputs_없음():
    spec = {"name": "x"}
    assert is_dual_output_spec(spec) is False


# ============================================================
# parse_and_validate_dual
# ============================================================


@pytest.mark.asyncio
async def test_parse_정상_2개_format():
    spec = {
        "name": "API 설계서",
        "outputs": [
            {"format": "markdown", "file_role": "human_review"},
            {"format": "openapi_yaml", "file_role": "developer_consumable",
             "schema_ref": None, "strict": False},
        ],
    }
    raw = """---FORMAT:markdown---
# API 설계서
내용

---FORMAT:openapi_yaml---
openapi: 3.1.0"""
    r = await parse_and_validate_dual(spec, raw)
    assert r.has_marker is True
    assert r.fallback_used is False
    assert len(r.parts) == 2
    assert r.by_role("human_review").format == "markdown"
    assert r.by_role("developer_consumable").format == "openapi_yaml"


@pytest.mark.asyncio
async def test_parse_fallback_마커_없음():
    spec = {
        "name": "X",
        "outputs": [
            {"format": "markdown", "file_role": "human_review"},
            {"format": "openapi_yaml", "file_role": "developer_consumable"},
        ],
    }
    raw = "그냥 일반 텍스트 — 마커 없음"
    r = await parse_and_validate_dual(spec, raw)
    assert r.has_marker is False
    assert r.fallback_used is True
    # fallback — 첫 outputs 항목에 raw 그대로
    assert len(r.parts) == 1
    assert r.parts[0].format == "markdown"
    assert r.parts[0].content == raw


@pytest.mark.asyncio
async def test_parse_strict_schema_검증():
    """strict + schema_ref 면 검증 + errors 기록."""
    spec = {
        "name": "InstantDB 데이터 모델 설계",
        "outputs": [
            {"format": "markdown", "file_role": "human_review"},
            {"format": "instantdb_schema", "file_role": "developer_consumable",
             "schema_ref": "schemas/instantdb_schema.json", "strict": True},
        ],
    }
    invalid_schema = json.dumps({"entities": {}})  # 빈 entities → minProperties 위반
    raw = f"---FORMAT:markdown---\n# md\n---FORMAT:instantdb_schema---\n{invalid_schema}"
    r = await parse_and_validate_dual(spec, raw)
    schema_part = r.by_role("developer_consumable")
    assert schema_part.validation_pass is False
    assert len(schema_part.validation_errors) > 0


@pytest.mark.asyncio
async def test_parse_strict_schema_정상_pass():
    spec = {
        "name": "InstantDB 데이터 모델 설계",
        "outputs": [
            {"format": "markdown", "file_role": "human_review"},
            {"format": "instantdb_schema", "file_role": "developer_consumable",
             "schema_ref": "schemas/instantdb_schema.json", "strict": True},
        ],
    }
    valid = json.dumps({
        "entities": {
            "users": {"fields": {"name": {"type": "string"}}},
        },
    })
    raw = f"---FORMAT:markdown---\n# md\n---FORMAT:instantdb_schema---\n{valid}"
    r = await parse_and_validate_dual(spec, raw)
    schema_part = r.by_role("developer_consumable")
    assert schema_part.validation_pass is True


# ============================================================
# directive
# ============================================================


def test_directive_마커_format_안내():
    d = dual_output_directive()
    assert "FORMAT:" in d
    assert "마커" in d
    assert "코드 블록" in d or "```" in d
