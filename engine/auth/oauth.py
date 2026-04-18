"""
engine/auth/oauth.py
OAuth 3사 토큰 교환 — Anthropic / OpenAI / Google.
aiohttp 기반 비동기 토큰 갱신.
JWT 서명: PyJWT (Google Service Account).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

_OAUTH_TIMEOUT = aiohttp.ClientTimeout(
    total=int(os.environ.get("V9_OAUTH_TIMEOUT", "30")),
    connect=int(os.environ.get("V9_OAUTH_CONNECT_TIMEOUT", "10")),
)


class OAuthError(Exception):
    """OAuth 토큰 교환/갱신 실패."""


# ---------------------------------------------------------------------------
# Google Service Account (JWT Bearer)
# ---------------------------------------------------------------------------

async def refresh_google_token(service_account_json: dict) -> dict:
    """
    Google Service Account JSON → 액세스 토큰 발급.
    범위: https://www.googleapis.com/auth/cloud-platform
    """
    try:
        import jwt as pyjwt
    except ImportError as exc:
        raise OAuthError("PyJWT 미설치: pip install PyJWT") from exc

    import json
    import base64

    sa = service_account_json
    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now,
    }

    # RS256 서명
    private_key = sa["private_key"]
    assertion = pyjwt.encode(claim, private_key, algorithm="RS256")

    async with aiohttp.ClientSession(timeout=_OAUTH_TIMEOUT) as session:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise OAuthError(f"Google 토큰 갱신 실패 [{resp.status}]: {text[:300]}")
            data = await resp.json()

    expires_at = datetime.fromtimestamp(now + data.get("expires_in", 3600), tz=timezone.utc)
    return {
        "access_token": data["access_token"],
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Anthropic OAuth (authorization_code / refresh_token)
# ---------------------------------------------------------------------------

async def exchange_anthropic_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_url: str = "https://api.anthropic.com/oauth/token",
) -> dict:
    """authorization_code → access_token + refresh_token."""
    async with aiohttp.ClientSession(timeout=_OAUTH_TIMEOUT) as session:
        async with session.post(
            token_url,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise OAuthError(f"Anthropic 코드 교환 실패 [{resp.status}]: {text[:300]}")
            return await resp.json()


async def refresh_anthropic_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    token_url: str = "https://api.anthropic.com/oauth/token",
) -> dict:
    """refresh_token → 새 access_token."""
    async with aiohttp.ClientSession(timeout=_OAUTH_TIMEOUT) as session:
        async with session.post(
            token_url,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise OAuthError(f"Anthropic 토큰 갱신 실패 [{resp.status}]: {text[:300]}")
            return await resp.json()


# ---------------------------------------------------------------------------
# OpenAI OAuth
# ---------------------------------------------------------------------------

async def refresh_openai_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    token_url: str = "https://api.openai.com/oauth/token",
) -> dict:
    async with aiohttp.ClientSession(timeout=_OAUTH_TIMEOUT) as session:
        async with session.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise OAuthError(f"OpenAI 토큰 갱신 실패 [{resp.status}]: {text[:300]}")
            return await resp.json()
