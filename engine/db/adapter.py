"""
engine/db/adapter.py
DatabaseAdapter 추상화 — SQLite / PostgreSQL 방언 분리.
핵심 원칙: 각 Adapter가 자기 방언을 직접 구현. 자동 번역 없음.
SQLite → PostgreSQL 전환 도구: pgloader
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 재진입 가능 asyncio Lock (begin_immediate 내부에서 execute 재호출 허용)
# ---------------------------------------------------------------------------

class _AsyncRLock:
    """Task-aware re-entrant asyncio Lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._count: int = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self._owner is task:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._count = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            raise RuntimeError("Lock not owned by current task")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "_AsyncRLock":
        await self.acquire()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class AdapterError(Exception):
    """DatabaseAdapter 기반 예외."""


class ConcurrentUpdateError(AdapterError):
    """낙관적 잠금 충돌: version 불일치로 UPDATE affected=0."""


# ---------------------------------------------------------------------------
# 추상 인터페이스
# ---------------------------------------------------------------------------

class DatabaseAdapter(ABC):
    """
    공통 DB 인터페이스.
    - 각 구현체는 자기 방언(json_extract/jsonb, datetime/now(), randomblob/gen_random_uuid 등)
      으로 직접 SQL을 작성한다.
    - 자동 방언 번역 모듈 없음.
    """

    @abstractmethod
    async def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        """단일 행 조회. 없으면 None."""

    @abstractmethod
    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """복수 행 조회."""

    @abstractmethod
    async def execute(self, query: str, params: tuple = ()) -> int:
        """DML 실행. 영향받은 행 수 반환."""

    @abstractmethod
    async def executemany(self, query: str, params_list: list[tuple]) -> None:
        """다중 DML 일괄 실행."""

    async def executescript(self, script: str) -> None:
        """
        여러 SQL 구문(트리거/DDL 포함)을 단일 스크립트로 실행.
        SQLite: conn.executescript() 사용 (BEGIN/END 블록 올바르게 처리).
        """
        raise NotImplementedError

    @abstractmethod
    @asynccontextmanager
    async def begin_immediate(self) -> AsyncGenerator[None, None]:
        """
        SQLite: BEGIN IMMEDIATE → DB 레벨 잠금 (row-level 불가).
        PostgreSQL: BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE.
        Circuit Breaker 등 낙관적 잠금이 필요한 구간에서 사용.
        """

    @abstractmethod
    @asynccontextmanager
    async def begin_serializable(self) -> AsyncGenerator[None, None]:
        """
        PostgreSQL: SERIALIZABLE isolation.
        SQLite에서는 begin_immediate와 동일 동작.
        """

    @abstractmethod
    async def close(self) -> None:
        """연결 정리."""


# ---------------------------------------------------------------------------
# SQLite 구현
# ---------------------------------------------------------------------------

class SQLiteAdapter(DatabaseAdapter):
    """
    SQLite WAL 모드 전용 Adapter.
    workers=1 단일 프로세스 환경에서만 사용.
    멀티 프로세스 환경 → PostgreSQLAdapter 사용.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = _AsyncRLock()

    async def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,   # autocommit 모드 (BEGIN은 직접 발행)
            )
            self._conn.row_factory = sqlite3.Row
            # WAL 모드 + 성능 설정 (schema_v8.sql PRAGMA 와 일치)
            self._conn.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                PRAGMA synchronous = NORMAL;
                PRAGMA cache_size = -64000;
                PRAGMA temp_store = MEMORY;
                PRAGMA busy_timeout = 300;
            """)
        return self._conn

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return dict(row)

    async def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        async with self._lock:
            conn = await self._get_conn()
            row = await asyncio.to_thread(lambda: conn.execute(query, params).fetchone())
            return self._row_to_dict(row)

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        async with self._lock:
            conn = await self._get_conn()
            rows = await asyncio.to_thread(lambda: [dict(r) for r in conn.execute(query, params).fetchall()])
            return rows

    async def execute(self, query: str, params: tuple = ()) -> int:
        async with self._lock:
            conn = await self._get_conn()
            rowcount = await asyncio.to_thread(lambda: conn.execute(query, params).rowcount)
            return rowcount

    async def executemany(self, query: str, params_list: list[tuple]) -> None:
        async with self._lock:
            conn = await self._get_conn()
            await asyncio.to_thread(lambda: conn.executemany(query, params_list))

    @asynccontextmanager
    async def begin_immediate(self) -> AsyncGenerator[None, None]:
        """
        BEGIN IMMEDIATE: DB 레벨 쓰기 잠금.
        SELECT FOR UPDATE 미지원 → 이 방식으로 원자적 read-modify-write.
        """
        async with self._lock:
            conn = await self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception as rb_exc:
                    logger.error("rollback_failed error=%s", rb_exc)
                raise

    @asynccontextmanager
    async def begin_serializable(self) -> AsyncGenerator[None, None]:
        """SQLite에서는 begin_immediate와 동일."""
        async with self.begin_immediate():
            yield

    async def executescript(self, script: str) -> None:
        """트리거/DDL 포함 스크립트 실행. executescript는 자동 COMMIT 처리."""
        async with self._lock:
            conn = await self._get_conn()
            conn.executescript(script)

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# PostgreSQL 구현 (asyncpg 기반 — 멀티 프로세스 환경)
# ---------------------------------------------------------------------------

class PostgreSQLAdapter(DatabaseAdapter):
    """
    asyncpg 기반 PostgreSQL Adapter.
    멀티 프로세스 / 고가용성 환경에서 사용.
    방언: jsonb, gen_random_uuid(), CURRENT_TIMESTAMP, SERIALIZABLE isolation.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None  # asyncpg.Pool

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg  # type: ignore
            except ImportError as exc:
                raise AdapterError(
                    "asyncpg 미설치. pip install asyncpg"
                ) from exc
            self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        return self._pool

    async def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return dict(row) if row else None

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def execute(self, query: str, params: tuple = ()) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(query, *params)
            # asyncpg returns "UPDATE N" string
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

    async def executemany(self, query: str, params_list: list[tuple]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(query, params_list)

    @asynccontextmanager
    async def begin_immediate(self) -> AsyncGenerator[None, None]:
        """PostgreSQL: SERIALIZABLE isolation (BEGIN IMMEDIATE 미지원)."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                yield

    @asynccontextmanager
    async def begin_serializable(self) -> AsyncGenerator[None, None]:
        async with self.begin_immediate():
            yield

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_adapter(db_url: str) -> DatabaseAdapter:
    """
    db_url 형식:
      sqlite:///path/to/file.db
      postgresql://user:pass@host/dbname
    """
    if db_url.startswith("sqlite:///"):
        path = db_url[len("sqlite:///"):]
        return SQLiteAdapter(path)
    elif db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        return PostgreSQLAdapter(db_url)
    else:
        raise AdapterError(f"지원하지 않는 DB URL 형식: {db_url}")
