"""
engine/lifecycle/shutdown.py
ShutdownManager — SIGTERM 수신 후 5단계 Graceful Shutdown.
Stage 1: 신규 작업 차단
Stage 2: 실행 중 에이전트 대기 (최대 25초)
Stage 3: 타임아웃 초과 에이전트 → SUSPENDED(SHUTDOWN_DRAIN)
Stage 4: DatabaseWriter drain (최대 5초)
Stage 5: WAL 체크포인트 + SystemShutdownCompleted 이벤트
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
import uuid
from datetime import datetime, timezone
from typing import Any

from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)


class ShutdownManager:
    """
    싱글턴으로 사용 (앱 당 1개).
    dag_advancer: DAGAdvancer 인스턴스
    db: DatabaseAdapter 인스턴스
    """

    AGENT_TIMEOUT = 25.0  # 초: 에이전트 완료 대기
    DRAIN_TIMEOUT = 5.0   # 초: DB drain

    def __init__(
        self,
        db: DatabaseAdapter,
        dag_advancer: Any | None = None,
    ) -> None:
        self._db = db
        self._dag_advancer = dag_advancer
        self._is_shutting_down = False
        self._active_tasks: dict[str, asyncio.Task] = {}   # node_id → Task

    # ------------------------------------------------------------------
    # 시그널 핸들러 등록
    # ------------------------------------------------------------------

    def setup_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 이벤트 루프가 아직 없는 경우 — 호출 시점 문제. 방어적 fallback.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(self.shutdown())
            )
        logger.info("shutdown_signal_handlers_registered")

    # ------------------------------------------------------------------
    # 활성 태스크 등록/해제
    # ------------------------------------------------------------------

    def register_task(self, node_id: str, task: asyncio.Task) -> None:
        self._active_tasks[node_id] = task

    def unregister_task(self, node_id: str) -> None:
        self._active_tasks.pop(node_id, None)

    @property
    def is_shutting_down(self) -> bool:
        return self._is_shutting_down

    # ------------------------------------------------------------------
    # 종료 절차
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        logger.info("shutdown_initiated active_tasks=%d", len(self._active_tasks))

        # Stage 1: 신규 작업 차단
        if self._dag_advancer:
            await self._dag_advancer.stop()
        logger.info("shutdown_stage1_new_tasks_blocked")

        # Stage 2~3: 에이전트 대기 + 타임아웃 처리
        completed, timed_out = await self._wait_for_agents(self.AGENT_TIMEOUT)
        if timed_out:
            await self._suspend_timed_out(timed_out)
        logger.info(
            "shutdown_stage3_agents_handled completed=%d suspended=%d",
            len(completed),
            len(timed_out),
        )

        # Stage 4: DB drain (5초 타임아웃)
        try:
            await asyncio.wait_for(self._db_drain(), timeout=self.DRAIN_TIMEOUT)
            logger.info("shutdown_stage4_db_drained")
        except asyncio.TimeoutError:
            logger.warning("shutdown_stage4_drain_timeout timeout=%s", self.DRAIN_TIMEOUT)

        # Stage 5: WAL 체크포인트 + 종료 이벤트
        await self._finalize(len(timed_out))
        logger.info("shutdown_complete")

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    async def _wait_for_agents(
        self, timeout: float
    ) -> tuple[list[str], list[str]]:
        """
        활성 태스크 완료 대기.
        반환: (완료된 node_id 목록, 타임아웃된 node_id 목록)
        """
        if not self._active_tasks:
            return [], []

        node_ids = list(self._active_tasks.keys())
        tasks = list(self._active_tasks.values())

        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
        except Exception as exc:
            logger.error("shutdown_wait_error error=%s", exc)
            return [], node_ids

        done_ids = [node_ids[tasks.index(t)] for t in done if t in tasks]
        pending_ids = [node_ids[tasks.index(t)] for t in pending if t in tasks]
        return done_ids, pending_ids

    async def _suspend_timed_out(self, node_ids: list[str]) -> None:
        """타임아웃 노드 취소 + SUSPENDED 상태 전이."""
        now = _now()
        for node_id in node_ids:
            task = self._active_tasks.get(node_id)
            if task and not task.done():
                task.cancel()
            await self._db.execute(
                """UPDATE nodes
                   SET state='SUSPENDED', suspension_reason='SHUTDOWN_DRAIN',
                       updated_at=?, version=version+1
                   WHERE id=? AND state='IN_PROGRESS'""",
                (now, node_id),
            )
            logger.info("shutdown_node_suspended node_id=%s", node_id)

    async def _db_drain(self) -> None:
        """대기 중인 DB 작업 완료 대기 (현재 단순 yield)."""
        await asyncio.sleep(0)  # 이벤트 루프에 제어 반환

    async def _finalize(self, suspended_count: int) -> None:
        """WAL 체크포인트 + 종료 이벤트 기록."""
        try:
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)", ())
        except Exception as exc:
            logger.warning("shutdown_wal_checkpoint_failed error=%s", exc)

        event_id = str(uuid.uuid4())
        payload = json.dumps({"suspended_count": suspended_count})
        checksum = hashlib.sha256(
            f"{event_id}SystemShutdownCompleted{payload}".encode()
        ).hexdigest()
        try:
            await self._db.execute(
                """INSERT INTO event_store
                   (event_id, aggregate_type, aggregate_id, event_type,
                    payload, checksum, actor, recorded_at)
                   VALUES (?, 'System', 'system', 'SystemShutdownCompleted',
                           ?, ?, 'SYSTEM', ?)""",
                (event_id, payload, checksum, _now()),
            )
        except Exception as exc:
            logger.warning("shutdown_event_store_failed error=%s", exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
