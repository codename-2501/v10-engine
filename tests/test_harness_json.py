"""S10: JSON harness 검증기 단위 테스트."""
from __future__ import annotations

import json

from engine.skills.qa.harness import _harness_validate_json


def _make_spec(min_items=0, forbidden=None):
    return {
        "validation": {
            "structural": {
                "min_items": min_items,
                "forbidden": forbidden or [],
            }
        }
    }


# ---------------------------------------------------------------------------
# PASS 케이스
# ---------------------------------------------------------------------------

def test_정상_JSON_배열_PASS():
    content = json.dumps([{"name": "a"}, {"name": "b"}, {"name": "c"}])
    r = _harness_validate_json(content, _make_spec(min_items=3))
    assert r["pass"] is True


def test_단일_객체_PASS():
    content = json.dumps({"name": "a", "css": ".a{}"})
    r = _harness_validate_json(content, _make_spec())
    assert r["pass"] is True


def test_빈_spec_PASS():
    content = json.dumps([{"x": 1}])
    r = _harness_validate_json(content, None)
    assert r["pass"] is True


# ---------------------------------------------------------------------------
# FAIL 케이스
# ---------------------------------------------------------------------------

def test_파싱_실패():
    r = _harness_validate_json("{invalid json", _make_spec())
    assert r["pass"] is False
    assert any("파싱 실패" in f for f in r["structural_failures"])


def test_빈_content():
    r = _harness_validate_json("", _make_spec())
    assert r["pass"] is False


def test_min_items_미달():
    content = json.dumps([{"name": "a"}])
    r = _harness_validate_json(content, _make_spec(min_items=5))
    assert r["pass"] is False
    assert any("원소 부족" in f for f in r["structural_failures"])


def test_incomplete_과다():
    items = [{"name": f"item{i}", "_incomplete": True} for i in range(5)]
    items += [{"name": "good"}]  # 5/6 = 83% incomplete
    content = json.dumps(items)
    r = _harness_validate_json(content, _make_spec())
    assert r["pass"] is False
    assert any("미완성" in f for f in r["structural_failures"])


def test_forbidden_키워드():
    content = json.dumps([{"name": "test", "desc": "TODO: 나중에 작성"}])
    r = _harness_validate_json(content, _make_spec(forbidden=["TODO"]))
    assert r["pass"] is False
    assert any("금지어" in f for f in r["structural_failures"])


def test_forbidden_없으면_PASS():
    content = json.dumps([{"name": "test", "desc": "정상 내용"}])
    r = _harness_validate_json(content, _make_spec(forbidden=["TODO", "TBD"]))
    assert r["pass"] is True


# ---------------------------------------------------------------------------
# 실전 케이스: v4 컴포넌트 레지스트리 (62원소, 0 incomplete)
# ---------------------------------------------------------------------------

def test_대형_배열_62원소():
    items = [{"page_slug": f"SC-AD-{i:03d}", "components": []} for i in range(62)]
    content = json.dumps(items)
    r = _harness_validate_json(content, _make_spec(min_items=10))
    assert r["pass"] is True
    assert any(c["name"] == "min_items" and c["count"] == 62 for c in r["checks"])
