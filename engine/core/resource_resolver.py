"""
engine/core/resource_resolver.py
ResourceResolver — 3계층 환경변수 조회 (PROJECT → ENGAGEMENT → GLOBAL).
암호화된 값은 AES-256-GCM 복호화 후 반환.
"""

from __future__ import annotations

import logging
import os

from engine.db.adapter import DatabaseAdapter
from engine.security.crypto import AES256GCM

logger = logging.getLogger(__name__)


# 노드 Phase별 필수 리소스 키
NODE_REQUIRED_KEYS: dict[str, list[str]] = {
    "DEFINE":         [],
    "DESIGN":         [],
    "BUILD":          ["GITHUB_TOKEN"],
    "VERIFY":         [],
    "DELIVER":        ["GITHUB_TOKEN", "DEPLOYMENT_TARGET"],
}


class MissingResourceError(Exception):
    """필수 리소스 키 누락."""
    def __init__(self, key: str, phase: str) -> None:
        super().__init__(f"필수 리소스 누락: key={key!r} (Phase={phase})")
        self.key = key
        self.phase = phase


class ResourceResolver:
    """
    PROJECT → ENGAGEMENT → GLOBAL 순서로 환경변수 조회.
    복호화: AES256GCM (PLATFORM_ENCRYPT_KEY 환경변수).
    """

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    async def resolve(
        self,
        key: str,
        project_id: str,
        engagement_id: str,
    ) -> str | None:
        """
        3계층 폴백 조회.
        반환: 복호화된 값 또는 None (없을 때).
        빈 문자열(placeholder)은 미설정으로 취급 → 다음 스코프로 폴백.
        """
        # 1. PROJECT 스코프
        val = await self._fetch("PROJECT", project_id, key)
        if val is not None and val.strip():
            return val

        # 2. ENGAGEMENT 스코프
        if engagement_id:
            val = await self._fetch("ENGAGEMENT", engagement_id, key)
            if val is not None and val.strip():
                return val

        # 3. GLOBAL 스코프
        val = await self._fetch("GLOBAL", "GLOBAL", key)
        if val is not None and val.strip():
            return val

        return None

    async def resolve_required(
        self,
        key: str,
        project_id: str,
        engagement_id: str,
        phase: str,
    ) -> str:
        """
        필수 리소스 조회. 없으면 MissingResourceError.
        """
        val = await self.resolve(key, project_id, engagement_id)
        if val is None:
            raise MissingResourceError(key, phase)
        return val

    async def validate_required_for_phase(
        self,
        phase: str,
        project_id: str,
        engagement_id: str,
    ) -> list[str]:
        """
        Phase 실행 전 필수 리소스 사전 검증.
        반환: 누락된 키 목록 (비어있으면 통과).
        """
        required = NODE_REQUIRED_KEYS.get(phase, [])
        missing = []
        for key in required:
            val = await self.resolve(key, project_id, engagement_id)
            if val is None:
                missing.append(key)
        return missing

    async def set(
        self,
        scope: str,
        scope_id: str,
        key: str,
        value: str,
        created_by: str,
        is_secret: bool = True,
        description: str = "",
    ) -> None:
        """
        환경변수 저장 (AES-256-GCM 암호화).
        scope: 'GLOBAL' | 'ENGAGEMENT' | 'PROJECT'
        """
        encrypt_key = AES256GCM.key_from_env()
        value_encrypted = AES256GCM.encrypt(value, encrypt_key)

        import uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        await self._db.execute(
            """INSERT INTO project_env_vars
               (id, scope, scope_id, key, value_encrypted,
                is_secret, description, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id, key)
               DO UPDATE SET value_encrypted=excluded.value_encrypted,
                             description=excluded.description""",
            (
                str(uuid.uuid4()), scope, scope_id, key, value_encrypted,
                1 if is_secret else 0, description, created_by, now,
            ),
        )

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    async def _fetch(self, scope: str, scope_id: str, key: str) -> str | None:
        row = await self._db.fetchone(
            """SELECT value_encrypted FROM project_env_vars
               WHERE scope=? AND scope_id=? AND key=?""",
            (scope, scope_id, key),
        )
        if not row:
            return None

        try:
            encrypt_key = AES256GCM.key_from_env()
            return AES256GCM.decrypt(row["value_encrypted"], encrypt_key)
        except Exception as exc:
            logger.error(
                "resource_decrypt_failed",
                scope=scope,
                scope_id=scope_id,
                key=key,
                error=str(exc),
            )
            return None
