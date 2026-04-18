"""Stage 10 _harness_validate_schema 테스트 (D8 Test Suite)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from engine.skills.qa.harness import _harness_validate_schema


def _mk_schema_file(tmp_path: Path, name: str, schema: dict) -> Path:
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir(exist_ok=True)
    p = schemas_dir / name
    p.write_text(json.dumps(schema), encoding="utf-8")
    return schemas_dir


def test_schema_미선언_skip():
    r = _harness_validate_schema("{}", {"name": "x"})
    assert r["pass"] is True
    assert r["schema_applied"] is False


def test_schema_pass(tmp_path):
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "number"}}}
    schemas_dir = _mk_schema_file(tmp_path, "s.json", schema)  # tmp_path/schemas
    spec = {"output_schema": {"type": "json", "schema_ref": "schemas/s.json"}}
    # schemas_dir 은 schemas/ 의 **부모** (함수가 schemas_dir/schemas/<ref>로 조합)
    r = _harness_validate_schema('{"a": 1}', spec, schemas_dir=str(schemas_dir))
    assert r["pass"] is True


def test_schema_fail_required_missing(tmp_path):
    schema = {"type": "object", "required": ["a"]}
    schemas_dir = _mk_schema_file(tmp_path, "s.json", schema)
    spec = {"output_schema": {"type": "json", "schema_ref": "schemas/s.json"}}
    r = _harness_validate_schema('{}', spec, schemas_dir=str(schemas_dir))
    assert r["pass"] is False
    assert "a" in " ".join(r["failures"]).lower() or len(r["failures"]) > 0


def test_schema_on_fail_필드_전달(tmp_path):
    schema = {"type": "object", "required": ["a"]}
    schemas_dir = _mk_schema_file(tmp_path, "s.json", schema)
    spec = {"output_schema": {
        "type": "json", "schema_ref": "schemas/s.json", "on_fail": "retry",
    }}
    r = _harness_validate_schema('{}', spec, schemas_dir=str(schemas_dir))
    assert r["on_fail"] == "retry"


def test_schema_invalid_json(tmp_path):
    schema = {"type": "object"}
    schemas_dir = _mk_schema_file(tmp_path, "s.json", schema)
    spec = {"output_schema": {"type": "json", "schema_ref": "schemas/s.json"}}
    r = _harness_validate_schema('not a json', spec, schemas_dir=str(schemas_dir))
    assert r["pass"] is False


def test_schema_file_not_found():
    spec = {"output_schema": {"type": "json", "schema_ref": "schemas/missing.json"}}
    r = _harness_validate_schema("{}", spec, schemas_dir="/tmp/none")
    assert r["pass"] is False
    assert any("schema" in f.lower() for f in r["failures"])


def test_schema_type_non_json_skip(tmp_path):
    schema = {"type": "object"}
    schemas_dir = _mk_schema_file(tmp_path, "s.json", schema)
    spec = {"output_schema": {"type": "html", "schema_ref": "schemas/s.json"}}
    r = _harness_validate_schema("<html></html>", spec, schemas_dir=str(schemas_dir))
    assert r["pass"] is True  # html 은 스킵
