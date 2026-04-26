"""B 회귀 — chunked_document sections-aware dual output 변환."""
from __future__ import annotations

from pathlib import Path

import yaml


SPECS_DIR = (
    Path(__file__).parent.parent
    / "engine" / "skills" / "specs" / "design"
)


def _load_spec(name: str) -> dict:
    with (SPECS_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# spec yaml dual output 정의
# ============================================================


def test_API_설계서_outputs_dual():
    spec = _load_spec("api_설계서.yaml")
    outs = spec.get("outputs", [])
    assert len(outs) == 2
    formats = [o["format"] for o in outs]
    assert "markdown" in formats
    assert "openapi_yaml" in formats


def test_DB_설계서_outputs_dual():
    spec = _load_spec("DB_설계서.yaml")
    outs = spec.get("outputs", [])
    assert len(outs) == 2
    formats = [o["format"] for o in outs]
    assert "sql_ddl" in formats


def test_상태_정의서_outputs_dual():
    spec = _load_spec("상태_정의서.yaml")
    outs = spec.get("outputs", [])
    assert len(outs) == 2
    formats = [o["format"] for o in outs]
    assert "state_machine_json" in formats


def test_보안_설계서_outputs_dual():
    spec = _load_spec("보안_설계서.yaml")
    outs = spec.get("outputs", [])
    assert len(outs) == 2
    formats = [o["format"] for o in outs]
    assert "iam_policy_json" in formats


def test_InstantDB_데이터_모델_outputs_dual():
    spec = _load_spec("instantdb_데이터_모델_설계.yaml")
    outs = spec.get("outputs", [])
    assert len(outs) == 2
    formats = [o["format"] for o in outs]
    assert "instantdb_schema" in formats


# ============================================================
# 마지막 section 에 dual output 마커 출력 명시
# ============================================================


def test_API_설계서_마지막_section_marker_명시():
    spec = _load_spec("api_설계서.yaml")
    last = spec["sections"][-1]
    assert "---FORMAT:openapi_yaml---" in last["outline"]


def test_DB_설계서_마지막_section_marker_명시():
    spec = _load_spec("DB_설계서.yaml")
    last = spec["sections"][-1]
    assert "---FORMAT:sql_ddl---" in last["outline"]


def test_상태_정의서_마지막_section_marker_명시():
    spec = _load_spec("상태_정의서.yaml")
    last = spec["sections"][-1]
    assert "---FORMAT:state_machine_json---" in last["outline"]


def test_보안_설계서_마지막_section_marker_명시():
    spec = _load_spec("보안_설계서.yaml")
    last = spec["sections"][-1]
    assert "---FORMAT:iam_policy_json---" in last["outline"]


# ============================================================
# chunked_document_generate 의 dual output wrapper 코드 존재
# ============================================================


def test_executor_dual_wrapper_존재():
    """chunked_document_generate 가 spec.outputs 면 ---FORMAT:markdown--- prepend."""
    import inspect
    from engine.skills import executor
    src = inspect.getsource(executor)
    # 정확한 wrapper 마커
    assert "---FORMAT:markdown---" in src
    assert "spec.get(\"outputs\")" in src or "spec.get('outputs')" in src


# ============================================================
# 시뮬레이션 — sections 결과 + 마커 → executor 분리 가능
# ============================================================


def test_sections_aware_분리_시나리오():
    """LLM 이 sections 합친 결과 + 마지막 section 에 dual output marker 출력 →
    executor 8-1C 가 정상 분리."""
    from engine.skills.qa.dual_output import split_by_marker
    sections_merged = (
        "---FORMAT:markdown---\n"
        "## API 목록\n표 1\n\n## 엔드포인트 상세\n표 2\n\n"
        "## 에러 코드\n표 3\n\n## 버전 관리\n버저닝 전략 ...\n\n"
        "---FORMAT:openapi_yaml---\n"
        "openapi: 3.1.0\ninfo:\n  title: Test\n  version: 1.0\npaths: {}"
    )
    parts = split_by_marker(sections_merged)
    assert len(parts) == 2
    assert parts[0][0] == "markdown"
    assert "API 목록" in parts[0][1]
    assert parts[1][0] == "openapi_yaml"
    assert "openapi: 3.1.0" in parts[1][1]
