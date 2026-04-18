"""S14: 디자인 토큰 → CSS 변수 변환 단위 테스트."""
from __future__ import annotations

import json
import pytest

from engine.skills.executor import _tokens_to_css_vars, _build_style_from_design_tokens


# ---------------------------------------------------------------------------
# _tokens_to_css_vars
# ---------------------------------------------------------------------------

def test_colors_평면_변환():
    tokens = {"colors": {"primary": "#C17840", "bg": "#111318", "text": "#e8eaf0"}}
    out = _tokens_to_css_vars(tokens)
    assert "--colors-primary: #C17840;" in out
    assert "--colors-bg: #111318;" in out


def test_중첩_타이포그래피():
    tokens = {"typography": {"body": {"size": "16px", "weight": "400"}}}
    out = _tokens_to_css_vars(tokens)
    assert "--typography-body-size: 16px;" in out
    assert "--typography-body-weight: 400;" in out


def test_혼합_구조():
    tokens = {
        "colors": {"primary": "#C17840"},
        "spacing": {"sm": "8px", "md": "16px"},
        "radius": {"button": "99px", "card": "16px"},
    }
    out = _tokens_to_css_vars(tokens)
    assert "--colors-primary" in out
    assert "--spacing-sm: 8px;" in out
    assert "--radius-button: 99px;" in out


def test_flat_키도_처리():
    tokens = {"accent": "#C17840", "colors": {"bg": "#000"}}
    out = _tokens_to_css_vars(tokens)
    assert "--accent: #C17840;" in out
    assert "--colors-bg: #000;" in out


def test_빈_dict():
    assert _tokens_to_css_vars({}) == ""


def test_non_dict_입력():
    assert _tokens_to_css_vars("string") == ""
    assert _tokens_to_css_vars(None) == ""


# ---------------------------------------------------------------------------
# _build_style_from_design_tokens
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, content=None):
        self._content = content

    async def fetchone(self, sql, params=None):
        if self._content is None:
            return None
        return {"content": self._content}


class _FakeNode:
    def __init__(self, pid="p1", id_="n1"):
        self.project_id = pid
        self.id = id_


@pytest.mark.asyncio
async def test_토큰_artifact_있으면_style_블록():
    tokens = {"colors": {"primary": "#C17840", "bg": "#111"}}
    db = _FakeDB(content=json.dumps(tokens))
    out = await _build_style_from_design_tokens(db, _FakeNode())
    assert out is not None
    assert "<style>" in out
    assert ":root" in out
    assert "--colors-primary: #C17840;" in out


@pytest.mark.asyncio
async def test_토큰_없으면_None():
    db = _FakeDB(content=None)
    out = await _build_style_from_design_tokens(db, _FakeNode())
    assert out is None


@pytest.mark.asyncio
async def test_JSON_파싱_실패_None():
    db = _FakeDB(content="not a json")
    out = await _build_style_from_design_tokens(db, _FakeNode())
    assert out is None


@pytest.mark.asyncio
async def test_db_None_None():
    out = await _build_style_from_design_tokens(None, _FakeNode())
    assert out is None


@pytest.mark.asyncio
async def test_빈_토큰_None():
    db = _FakeDB(content=json.dumps({}))
    out = await _build_style_from_design_tokens(db, _FakeNode())
    # 빈 tokens → css_vars 빈 문자열 → None 반환
    assert out is None
