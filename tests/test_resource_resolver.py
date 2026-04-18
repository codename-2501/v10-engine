"""
tests/test_resource_resolver.py
ResourceResolver PROJECT → ENGAGEMENT → GLOBAL fallback 체인 테스트.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 헬퍼: DB mock
# ---------------------------------------------------------------------------

def _make_db(
    project_val: str | None = None,
    engagement_val: str | None = None,
    global_val: str | None = None,
) -> MagicMock:
    """
    fetchone 호출 순서: PROJECT → ENGAGEMENT → GLOBAL
    각 단계별로 값 반환/None 반환.
    """
    db = MagicMock()

    async def fetchone(query, params=()):
        if not params:
            return None
        scope = params[0]
        if scope == "PROJECT":
            return {"value_encrypted": project_val} if project_val else None
        if scope == "ENGAGEMENT":
            return {"value_encrypted": engagement_val} if engagement_val else None
        if scope == "GLOBAL":
            return {"value_encrypted": global_val} if global_val else None
        return None

    db.fetchone = AsyncMock(side_effect=fetchone)
    db.execute = AsyncMock(return_value=1)
    return db


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

def test_resolves_from_project_first() -> None:
    """PROJECT 레벨에 값이 있으면 바로 반환."""
    from engine.core.resource_resolver import ResourceResolver

    db = _make_db(project_val="proj_secret")
    resolver = ResourceResolver(db)

    with patch("engine.core.resource_resolver.AES256GCM.key_from_env", return_value=b"k" * 32), \
         patch("engine.core.resource_resolver.AES256GCM.decrypt", side_effect=lambda v, k: v):
        result = asyncio.get_event_loop().run_until_complete(
            resolver.resolve("API_KEY", "proj-1", "eng-1")
        )
    assert result == "proj_secret"


def test_fallback_to_engagement() -> None:
    """PROJECT 없으면 ENGAGEMENT에서 가져옴."""
    from engine.core.resource_resolver import ResourceResolver

    db = _make_db(project_val=None, engagement_val="eng_secret")
    resolver = ResourceResolver(db)

    with patch("engine.core.resource_resolver.AES256GCM.key_from_env", return_value=b"k" * 32), \
         patch("engine.core.resource_resolver.AES256GCM.decrypt", side_effect=lambda v, k: v):
        result = asyncio.get_event_loop().run_until_complete(
            resolver.resolve("API_KEY", "proj-1", "eng-1")
        )
    assert result == "eng_secret"


def test_fallback_to_global() -> None:
    """PROJECT/ENGAGEMENT 없으면 GLOBAL에서 가져옴."""
    from engine.core.resource_resolver import ResourceResolver

    db = _make_db(project_val=None, engagement_val=None, global_val="global_secret")
    resolver = ResourceResolver(db)

    with patch("engine.core.resource_resolver.AES256GCM.key_from_env", return_value=b"k" * 32), \
         patch("engine.core.resource_resolver.AES256GCM.decrypt", side_effect=lambda v, k: v):
        result = asyncio.get_event_loop().run_until_complete(
            resolver.resolve("API_KEY", "proj-1", "eng-1")
        )
    assert result == "global_secret"


def test_returns_none_if_not_found() -> None:
    """어느 레벨에도 없으면 None."""
    from engine.core.resource_resolver import ResourceResolver

    db = _make_db()
    resolver = ResourceResolver(db)

    result = asyncio.get_event_loop().run_until_complete(
        resolver.resolve("MISSING_KEY", "proj-1", "eng-1")
    )
    assert result is None


def test_resolve_required_raises_if_missing() -> None:
    """필수 키 없으면 MissingResourceError."""
    from engine.core.resource_resolver import ResourceResolver, MissingResourceError

    db = _make_db()
    resolver = ResourceResolver(db)

    with pytest.raises(MissingResourceError):
        asyncio.get_event_loop().run_until_complete(
            resolver.resolve_required("GITHUB_TOKEN", "proj-1", "eng-1", "BUILD")
        )
