"""Phase 1-3 회귀 — schema 검증 실패 시 retry_with_feedback."""
from __future__ import annotations

import json

import pytest

from engine.skills.qa.schema_validator import (
    build_retry_prompt,
    validate_against_schema,
    validate_and_retry,
)


class _FakeAdapter:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def call(self, model, prompt, max_tokens, system="", **kwargs):
        self.calls.append({"prompt": prompt, "system": system})
        # mock APIResponse
        class _R:
            content = ""
            input_tokens = 0
            output_tokens = 0
            model = ""
            stop_reason = "end_turn"
        r = _R()
        r.content = self._responses.pop(0) if self._responses else ""
        return r


def test_build_retry_prompt_errors_포함():
    p = build_retry_prompt(
        original_prompt="원래 spec",
        errors=["[pages] required", "[components/0/name] pattern mismatch"],
        schema_ref="schemas/x.json",
    )
    assert "schema 검증 실패" in p
    assert "pattern mismatch" in p
    assert "원래 spec" in p


@pytest.mark.asyncio
async def test_strict_없으면_skip():
    """spec.output_schema 없으면 검증 안 함."""
    spec = {"name": "x"}  # output_schema 없음
    adapter = _FakeAdapter([])
    content, vr = await validate_and_retry(
        content='not json',
        spec=spec,
        model_adapter=adapter,
        model="sonnet",
        original_prompt="p",
    )
    assert vr.pass_ is True
    assert len(adapter.calls) == 0


@pytest.mark.asyncio
async def test_정상_pass_시_retry_안함():
    spec = {
        "name": "registry",
        "output_schema": {
            "schema_ref": "schemas/component_registry.json",
            "strict": True,
            "on_fail": "retry_with_feedback",
            "max_retries": 1,
        },
    }
    valid = json.dumps({
        "pages": [{
            "page_name": "홈", "page_slug": "home",
            "components": [{"component_name": "page_header"}],
        }],
    })
    adapter = _FakeAdapter([])
    content, vr = await validate_and_retry(
        content=valid, spec=spec, model_adapter=adapter,
        model="sonnet", original_prompt="p",
    )
    assert vr.pass_ is True
    assert len(adapter.calls) == 0  # retry 안 함


@pytest.mark.asyncio
async def test_FAIL_시_1회_retry_성공():
    spec = {
        "name": "registry",
        "output_schema": {
            "schema_ref": "schemas/component_registry.json",
            "strict": True,
            "on_fail": "retry_with_feedback",
            "max_retries": 1,
        },
    }
    invalid = json.dumps({"pages": []})  # 빈 pages → schema fail
    valid_retry = json.dumps({
        "pages": [{
            "page_name": "홈", "page_slug": "home",
            "components": [{"component_name": "page_header"}],
        }],
    })
    adapter = _FakeAdapter([valid_retry])
    content, vr = await validate_and_retry(
        content=invalid, spec=spec, model_adapter=adapter,
        model="sonnet", original_prompt="p",
    )
    assert vr.pass_ is True
    assert len(adapter.calls) == 1
    assert "schema 검증 실패" in adapter.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_FAIL_max_retries_소진():
    spec = {
        "name": "registry",
        "output_schema": {
            "schema_ref": "schemas/component_registry.json",
            "strict": True,
            "on_fail": "retry_with_feedback",
            "max_retries": 1,
        },
    }
    invalid_1 = json.dumps({"pages": []})
    invalid_2 = json.dumps({"pages": []})
    adapter = _FakeAdapter([invalid_2])
    content, vr = await validate_and_retry(
        content=invalid_1, spec=spec, model_adapter=adapter,
        model="sonnet", original_prompt="p",
    )
    assert vr.pass_ is False
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_on_fail_warn_은_retry_안함():
    """on_fail 이 retry_with_feedback 이 아니면 retry 안 함 (graceful degrade)."""
    spec = {
        "name": "registry",
        "output_schema": {
            "schema_ref": "schemas/component_registry.json",
            "strict": True,
            "on_fail": "warn",
        },
    }
    invalid = json.dumps({"pages": []})
    adapter = _FakeAdapter([])
    content, vr = await validate_and_retry(
        content=invalid, spec=spec, model_adapter=adapter,
        model="sonnet", original_prompt="p",
    )
    assert vr.pass_ is False
    assert len(adapter.calls) == 0
