"""A1 회귀 — CLIProxyAdapter binary 사전 검증 (fail-fast)."""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from engine.ai.model_adapter import CLIProxyAdapter


def test_절대경로_부재_시_RuntimeError():
    with pytest.raises(RuntimeError, match="Claude CLI 바이너리 누락"):
        CLIProxyAdapter(cli_path="/nonexistent/path/claude")


def test_PATH_미존재_시_RuntimeError(monkeypatch):
    monkeypatch.setenv("PATH", "/this/dir/does/not/exist")
    with pytest.raises(RuntimeError, match="PATH 에서"):
        CLIProxyAdapter(cli_path="claude_zzz_does_not_exist")


def test_절대경로_존재시_정상_생성():
    # 임시 실행 가능 binary 생성
    with tempfile.NamedTemporaryFile(
        prefix="claude_test_", suffix="", delete=False
    ) as tmp:
        tmp.write(b"#!/bin/sh\necho test\n")
        path = tmp.name
    os.chmod(path, 0o755)
    try:
        adapter = CLIProxyAdapter(cli_path=path)
        assert adapter._cli == path
    finally:
        os.unlink(path)


def test_PATH_resolve_시_정상_변환():
    # /bin/sh 같은 PATH 에서 항상 찾는 binary 로 모의
    sh_path = shutil.which("sh")
    if not sh_path:
        pytest.skip("sh 없음")
    adapter = CLIProxyAdapter(cli_path="sh")
    assert adapter._cli == sh_path
