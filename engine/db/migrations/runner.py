"""
engine/db/migrations/runner.py
마이그레이션 프레임워크 — schema_migrations 테이블 기반 순차 적용.
파일 형식: versions/<version>.sql
  -- UP:
  CREATE TABLE ...
  -- DOWN:
  DROP TABLE ...
실패 시 즉시 롤백. SHA-256 체크섬 기록.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)

VERSIONS_DIR = Path(__file__).parent / "versions"
_VERSION_RE = re.compile(r"^(\d{3}_\w+)\.sql$")


class MigrationError(Exception):
    pass


class MigrationRunner:
    """
    마이그레이션 적용 순서:
      1. schema_migrations 테이블 확보
      2. 적용 완료 목록 조회
      3. 미적용 버전 파일 순서대로 UP 섹션 실행
      4. 성공 시 schema_migrations에 버전 + SHA-256 기록
      5. 실패 시 ROLLBACK
    """

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    async def run_pending(self) -> list[str]:
        """
        미적용 마이그레이션 순차 실행.
        반환: 이번 실행에서 적용된 버전 목록.
        """
        await self._ensure_migrations_table()
        applied = await self._get_applied_versions()
        pending = self._discover_pending(applied)

        applied_now: list[str] = []
        for version, sql_path in pending:
            await self._apply(version, sql_path)
            applied_now.append(version)

        if applied_now:
            logger.info("migrations_applied versions=%s", applied_now)
        else:
            logger.info("migrations_up_to_date")

        return applied_now

    async def rollback_last(self) -> str | None:
        """
        가장 최근 적용 마이그레이션의 DOWN 섹션 실행.
        반환: 롤백된 버전명 (없으면 None).
        """
        await self._ensure_migrations_table()
        row = await self._db.fetchone(
            "SELECT version FROM schema_migrations ORDER BY applied_at DESC LIMIT 1"
        )
        if not row:
            return None

        version = row["version"]
        sql_path = VERSIONS_DIR / f"{version}.sql"
        if not sql_path.exists():
            raise MigrationError(f"마이그레이션 파일 없음: {sql_path}")

        down_sql = _parse_section(sql_path.read_text("utf-8"), "DOWN")
        if not down_sql.strip():
            raise MigrationError(f"{version}: DOWN 섹션 비어있음")

        async with self._db.begin_immediate():
            for stmt in _split_statements(down_sql):
                await self._db.execute(stmt)
            await self._db.execute(
                "DELETE FROM schema_migrations WHERE version=?", (version,)
            )

        logger.info("migration_rolled_back version=%s", version)
        return version

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    async def _ensure_migrations_table(self) -> None:
        """schema_migrations 테이블 없으면 생성 (최초 실행 시)."""
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version     TEXT PRIMARY KEY,
              description TEXT NOT NULL,
              checksum    TEXT NOT NULL,
              applied_at  TEXT NOT NULL
            )
        """)

    async def _get_applied_versions(self) -> set[str]:
        rows = await self._db.fetchall("SELECT version FROM schema_migrations")
        return {r["version"] for r in rows}

    def _discover_pending(
        self, applied: set[str]
    ) -> list[tuple[str, Path]]:
        """versions/ 디렉토리에서 미적용 SQL 파일 목록을 버전 순 정렬로 반환."""
        pending = []
        if not VERSIONS_DIR.exists():
            return pending

        for fname in sorted(os.listdir(VERSIONS_DIR)):
            m = _VERSION_RE.match(fname)
            if not m:
                continue
            version = m.group(1)
            if version not in applied:
                pending.append((version, VERSIONS_DIR / fname))
        return pending

    async def _apply(self, version: str, sql_path: Path) -> None:
        """단일 마이그레이션 UP 섹션 적용."""
        raw = sql_path.read_text("utf-8")
        up_sql = _parse_section(raw, "UP")
        if not up_sql.strip():
            raise MigrationError(f"{version}: UP 섹션 비어있음")

        checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        description = _extract_description(raw) or version

        # executescript: COMMIT-before-run → 트리거/DDL/PRAGMA 모두 올바르게 처리
        # 마이그레이션 기록은 별도 트랜잭션으로 삽입
        await self._db.executescript(up_sql)
        async with self._db.begin_immediate():
            await self._db.execute(
                """INSERT INTO schema_migrations (version, description, checksum, applied_at)
                   VALUES (?, ?, ?, ?)""",
                (version, description, checksum, _now()),
            )

        logger.info("migration_applied version=%s checksum=%s", version, checksum[:12])


# ---------------------------------------------------------------------------
# SQL 파싱 유틸
# ---------------------------------------------------------------------------

def _parse_section(sql: str, section: str) -> str:
    """
    '-- UP:' 또는 '-- DOWN:' 섹션 추출.
    섹션이 없으면 빈 문자열 반환.
    """
    pattern = re.compile(
        rf"--\s*{section}:\s*\n(.*?)(?=--\s*(?:UP|DOWN):|$)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(sql)
    return m.group(1) if m else ""


def _split_statements(sql: str) -> list[str]:
    """
    세미콜론(;) 기준으로 SQL 구문 분리.
    빈 구문 제거.
    """
    stmts = []
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            stmts.append(stmt)
    return stmts


def _extract_description(sql: str) -> str:
    """파일 첫 줄 주석에서 설명 추출."""
    for line in sql.splitlines():
        line = line.strip()
        if line.startswith("--"):
            desc = line.lstrip("-").strip()
            if desc:
                return desc[:200]
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
