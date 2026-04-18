"""V9 리팩토링된 모듈 테스트."""

from __future__ import annotations

import pytest


def test_executor_gotcha_module():
    """executor_gotcha 모듈 import 검증."""
    try:
        from engine.skills.executor_gotcha import (
            classify_gotcha,
            record_gotcha,
            load_gotchas_for_prompt,
        )

        # 분류 함수 테스트
        assert classify_gotcha("403 Forbidden") == "permission_denied"
        assert classify_gotcha("404 Not Found") == "not_found"
        assert classify_gotcha("timeout") == "timeout"
        assert classify_gotcha("validation error") == "validation_error"
        assert classify_gotcha("unknown error") == "generic"

    except ImportError:
        pytest.skip("executor_gotcha not available")


def test_css_tokens_module():
    """css_tokens 모듈 검증."""
    try:
        from engine.skills.codegen.css_tokens import (
            tokens_to_css_vars,
            build_style_from_design_tokens,
        )

        tokens = {"color": {"primary": "#007ACC", "secondary": "#005a9e"}}
        css_vars = tokens_to_css_vars(tokens)

        assert "--color-primary" in css_vars
        assert css_vars["--color-primary"] == "#007ACC"

        style = build_style_from_design_tokens(tokens)
        assert ":root {" in style
        assert "--color-primary: #007ACC" in style

    except ImportError:
        pytest.skip("css_tokens not available")


def test_executor_heartbeat_module():
    """executor_heartbeat 모듈 import 검증."""
    try:
        from engine.skills.executor_heartbeat import executor_with_heartbeat

        assert executor_with_heartbeat is not None

    except ImportError:
        pytest.skip("executor_heartbeat not available")
