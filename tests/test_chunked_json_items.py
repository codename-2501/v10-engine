"""S8 범용 JSON chunk_items 분할 단위 테스트."""
from __future__ import annotations

import json
import pytest

from engine.skills.executor import _chunked_json_items_generate


class _FakeResp:
    def __init__(self, content, in_t=100, out_t=200):
        self.content = content
        self.input_tokens = in_t
        self.output_tokens = out_t
        self.stop_reason = "end_turn"


class _FakeAdapter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def call(self, model, system, prompt, max_tokens):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if not self._responses:
            return _FakeResp("{}")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return _FakeResp(r)


class _FakeNode:
    def __init__(self, id="n1", name="test"):
        self.id = id
        self.name = name


class _FakeAssembly:
    def __init__(self, prompt="base prompt with {{chunk_item}}"):
        self.prompt = prompt
        self.system = "sys"


# ---------------------------------------------------------------------------
# 핵심 flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_9개_아이템_각_1회_호출():
    items = ["toast", "modal", "confirm_dialog"]
    responses = [
        '{"name":"toast","css":".t{}"}',
        '{"name":"modal","css":".m{}"}',
        '{"name":"confirm_dialog","css":".c{}"}',
    ]
    adapter = _FakeAdapter(responses)
    spec = {"chunk_items": items, "model_preference": "sonnet"}
    node = _FakeNode()
    assembly = _FakeAssembly()

    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    assert len(adapter.calls) == 3
    parsed = json.loads(resp.content)
    assert len(parsed) == 3
    assert parsed[0]["name"] == "toast"
    assert parsed[1]["name"] == "modal"
    # input/output 합산
    assert resp.input_tokens == 300
    assert resp.output_tokens == 600


@pytest.mark.asyncio
async def test_prompt에_chunk_item_치환():
    adapter = _FakeAdapter(['{"name":"toast"}'])
    spec = {"chunk_items": ["toast"]}
    node = _FakeNode()
    assembly = _FakeAssembly(prompt="generate {{chunk_item}} component")

    await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    # prompt 에 chunk_item 치환됨 + 엄격 지시 블록 추가
    assert "generate toast component" in adapter.calls[0]["prompt"]
    assert "오직 'toast'" in adapter.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_실패_아이템_placeholder():
    adapter = _FakeAdapter([
        '{"name":"toast"}',
        Exception("API error"),
        '{"name":"confirm_dialog"}',
    ])
    spec = {"chunk_items": ["toast", "modal", "confirm_dialog"]}
    node = _FakeNode()
    assembly = _FakeAssembly()

    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    parsed = json.loads(resp.content)
    assert len(parsed) == 3
    # 2번째는 placeholder
    assert parsed[1].get("_incomplete") is True
    assert parsed[1]["name"] == "modal"


@pytest.mark.asyncio
async def test_배열로_반환도_수용():
    """LLM 이 실수로 [{...}] 반환해도 첫 원소 수용."""
    adapter = _FakeAdapter(['[{"name":"toast","css":".t{}"}]'])
    spec = {"chunk_items": ["toast"]}
    node = _FakeNode()
    assembly = _FakeAssembly()

    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    parsed = json.loads(resp.content)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "toast"


@pytest.mark.asyncio
async def test_코드블록_감싸기_제거():
    """LLM 이 ```json ... ``` 으로 감싸도 파싱."""
    adapter = _FakeAdapter(['```json\n{"name":"toast"}\n```'])
    spec = {"chunk_items": ["toast"]}
    node = _FakeNode()
    assembly = _FakeAssembly()

    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    parsed = json.loads(resp.content)
    assert parsed[0]["name"] == "toast"


@pytest.mark.asyncio
async def test_빈_chunk_items():
    """chunk_items 없으면 루프 skip → 빈 배열."""
    adapter = _FakeAdapter([])
    spec = {"chunk_items": []}
    node = _FakeNode()
    assembly = _FakeAssembly()
    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    assert resp.content == "[]"
    assert len(adapter.calls) == 0


@pytest.mark.asyncio
async def test_non_string_item_skip():
    """int 등 비문자열 item 은 skip."""
    adapter = _FakeAdapter(['{"name":"toast"}'])
    spec = {"chunk_items": ["toast", 42, None]}
    node = _FakeNode()
    assembly = _FakeAssembly()
    resp = await _chunked_json_items_generate(adapter, assembly, spec, node, db=None)
    parsed = json.loads(resp.content)
    assert len(parsed) == 1
    assert len(adapter.calls) == 1
