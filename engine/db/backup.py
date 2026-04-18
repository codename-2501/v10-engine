"""
engine/db/backup.py
SQLite 백업 전략 (독립 모듈).
StartupRecovery.BackupScheduler와 별개로 직접 호출 가능.

일일 자동 백업 절차:
1. WAL 체크포인트: PRAGMA wal_checkpoint(TRUNCATE)
2. VACUUM INTO 'backups/platform_YYYYMMDD_HHMMSS.db'
3. PRAGMA integrity_check → 'ok' 아니면 SystemBackupFailed + 알림
4. SHA-256(백업 파일) 체크섬 기록
5. tar -czf backups/artifacts_YYYYMMDD.tar.gz ./artifacts/
6. 30일 초과 백업 자동 삭제
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("backups")
ARTIFACTS_DIR = Path("artifacts")
RETENTION_DAYS = 30
BACKUP_HOUR = 3   # 03:00 UTC


# ---------------------------------------------------------------------------
# 단발 백업
# ---------------------------------------------------------------------------

async def run_backup(db: DatabaseAdapter, db_path: str = "platform.db") -> dict:
    """
    즉시 백업 실행.
    반환: {"backup_path": str, "checksum": str, "artifact_archive": str | None}
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    backup_db_path = BACKUP_DIR / f"platform_{ts}.db"
    artifact_archive = BACKUP_DIR / f"artifacts_{now.strftime('%Y%m%d')}.tar.gz"

    # 1. WAL 체크포인트
    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)", ())
        logger.info("backup_wal_checkpoint_done")
    except Exception as exc:
        logger.warning("backup_wal_checkpoint_failed error=%s", str(exc))

    # 2. VACUUM INTO (SQLite only)
    try:
        await db.execute(f"VACUUM INTO '{backup_db_path}'", ())
        logger.info("backup_vacuum_into_done path=%s", str(backup_db_path))
    except Exception as exc:
        # PostgreSQL 등 non-SQLite: 파일 복사 시도
        if os.path.exists(db_path):
            shutil.copy2(db_path, backup_db_path)
            logger.info("backup_file_copy_done path=%s", str(backup_db_path))
        else:
            await _emit_backup_failed(db, str(exc))
            raise

    # 3. Integrity check
    integrity_ok = await _check_integrity(db)
    if not integrity_ok:
        msg = "integrity_check 실패"
        await _emit_backup_failed(db, msg)
        raise RuntimeError(msg)

    # 4. SHA-256 체크섬
    checksum = _sha256(backup_db_path)
    checksum_path = BACKUP_DIR / f"platform_{ts}.db.sha256"
    checksum_path.write_text(f"{checksum}  {backup_db_path.name}\n", encoding="utf-8")
    logger.info("backup_checksum_recorded checksum=%s", checksum[:16] + "…")

    # 5. artifacts 아카이브 (존재할 경우)
    archive_path: str | None = None
    if ARTIFACTS_DIR.is_dir():
        try:
            with tarfile.open(artifact_archive, "w:gz") as tar:
                tar.add(ARTIFACTS_DIR, arcname="artifacts")
            archive_path = str(artifact_archive)
            logger.info("backup_artifacts_archived path=%s", archive_path)
        except Exception as exc:
            logger.warning("backup_artifacts_archive_failed error=%s", str(exc))

    # 6. 30일 초과 파일 삭제
    _cleanup_old_backups()

    # 성공 이벤트 기록
    await _emit_backup_completed(db, str(backup_db_path), checksum)

    return {
        "backup_path": str(backup_db_path),
        "checksum": checksum,
        "artifact_archive": archive_path,
    }


# ---------------------------------------------------------------------------
# 스케줄러 (asyncio 루프)
# ---------------------------------------------------------------------------

class BackupScheduler:
    """
    매일 BACKUP_HOUR시(UTC)에 자동 백업 실행.
    engine/lifecycle/startup.py의 BackupScheduler와 동일한 역할 (대체 가능).
    """

    def __init__(self, db: DatabaseAdapter, db_path: str = "platform.db") -> None:
        self._db = db
        self._db_path = db_path
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        logger.info("backup_scheduler_started hour=%s", BACKUP_HOUR)
        while self._running:
            await _wait_until_hour(BACKUP_HOUR)
            if not self._running:
                break
            try:
                result = await run_backup(self._db, self._db_path)
                logger.info("scheduled_backup_done backup_path=%s checksum=%s artifact_archive=%s", result["backup_path"], result["checksum"], result["artifact_archive"])
            except Exception as exc:
                logger.error("scheduled_backup_failed error=%s", str(exc), exc_info=True)
            # 다음 실행은 24시간 후
            await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

async def _check_integrity(db: DatabaseAdapter) -> bool:
    try:
        row = await db.fetchone("PRAGMA integrity_check")
        result = row[0] if row else "fail"
        return result == "ok"
    except Exception as exc:
        logger.error("integrity_check_error error=%s", str(exc))
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_old_backups() -> None:
    """30일 초과 .db 및 .tar.gz 파일 삭제."""
    import time
    cutoff = time.time() - RETENTION_DAYS * 86400
    deleted = 0
    for p in BACKUP_DIR.glob("platform_*.db"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            sha = p.with_suffix(".db.sha256")
            sha.unlink(missing_ok=True)
            deleted += 1
    for p in BACKUP_DIR.glob("artifacts_*.tar.gz"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            deleted += 1
    if deleted:
        logger.info("backup_cleanup_done deleted=%s", deleted)


async def _wait_until_hour(target_hour: int) -> None:
    """다음 target_hour(UTC) 정각까지 대기."""
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if next_run <= now:
        from datetime import timedelta
        next_run += timedelta(days=1)
    wait_secs = (next_run - now).total_seconds()
    await asyncio.sleep(wait_secs)


async def _emit_backup_completed(
    db: DatabaseAdapter, backup_path: str, checksum: str
) -> None:
    try:
        await db.execute(
            """INSERT INTO event_store (event_type, aggregate_type, aggregate_id, payload, created_at)
               VALUES ('SystemBackupCompleted', 'System', 'system',
                       json_object('backup_path', ?, 'checksum', ?), datetime('now'))""",
            (backup_path, checksum),
        )
    except Exception as exc:
        logger.warning("backup_event_insert_failed error=%s", str(exc))


async def _emit_backup_failed(db: DatabaseAdapter, reason: str) -> None:
    try:
        await db.execute(
            """INSERT INTO event_store (event_type, aggregate_type, aggregate_id, payload, created_at)
               VALUES ('SystemBackupFailed', 'System', 'system',
                       json_object('reason', ?), datetime('now'))""",
            (reason,),
        )
    except Exception as exc:
        logger.warning("backup_failed_event_insert_failed error=%s", str(exc))
