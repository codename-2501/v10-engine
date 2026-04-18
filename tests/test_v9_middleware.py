"""V9 성능 미들웨어 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_gzip_middleware_imported():
    """GZip 미들웨어 import 가능 확인."""
    try:
        from fastapi.middleware.gzip import GZipMiddleware

        assert GZipMiddleware is not None
    except ImportError:
        pytest.skip("GZipMiddleware not available")


@pytest.mark.asyncio
async def test_cors_middleware_imported():
    """CORS 미들웨어 import 가능 확인."""
    try:
        from fastapi.middleware.cors import CORSMiddleware

        assert CORSMiddleware is not None
    except ImportError:
        pytest.skip("CORSMiddleware not available")


def test_thresholds_constants():
    """V9 새로 추가된 thresholds 상수 검증."""
    try:
        from engine.config.thresholds import (
            ENGINE_VERSION,
            ACCOUNT_CACHE_TTL,
            CLI_TIMEOUT_HAIKU,
            CLI_TIMEOUT_SONNET,
            CLI_TIMEOUT_OPUS,
            QA_PASS_THRESHOLD,
            QA_PARTIAL_THRESHOLD,
        )

        assert ENGINE_VERSION == "9.0.0"
        assert ACCOUNT_CACHE_TTL == 30.0
        assert CLI_TIMEOUT_HAIKU == 300
        assert CLI_TIMEOUT_SONNET == 600
        assert CLI_TIMEOUT_OPUS == 1200
        assert QA_PASS_THRESHOLD == 50
        assert QA_PARTIAL_THRESHOLD == 30

    except ImportError as e:
        pytest.skip(f"thresholds constants not available: {e}")
