"""
api/websocket.py
WebSocket 실시간 통신 — 인게이지먼트 이벤트 스트림.
WS /ws/engagements/{id}/stream
→ Outbox의 destination='WEBSOCKET' 이벤트 구독
→ NodeStateChanged, GateTriggered, AgentProcessUpdated, BudgetWarning, CBStateChanged 등

연결 관리: ConnectionManager (engagement_id별 브로드캐스트)
Outbox 폴링: 5초 주기, last_id 기반 증분 조회
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """
    engagement_id → 활성 WebSocket 연결 목록 관리.
    브로드캐스트: 특정 engagement의 모든 연결에 메시지 전송.
    """

    def __init__(self) -> None:
        # engagement_id → set of WebSocket
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, websocket: WebSocket, engagement_id: str) -> None:
        await websocket.accept()
        self._connections[engagement_id].add(websocket)
        logger.info("ws_client_connected engagement_id=%s total=%d",
                    engagement_id, len(self._connections[engagement_id]))

    def disconnect(self, websocket: WebSocket, engagement_id: str) -> None:
        self._connections[engagement_id].discard(websocket)
        if not self._connections[engagement_id]:
            del self._connections[engagement_id]
        logger.info("ws_client_disconnected engagement_id=%s", engagement_id)

    async def broadcast(self, engagement_id: str, message: dict) -> None:
        """해당 engagement의 모든 연결에 JSON 메시지 전송."""
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(engagement_id, set())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, engagement_id)

    def active_engagements(self) -> list[str]:
        return list(self._connections.keys())

    def connection_count(self, engagement_id: str) -> int:
        return len(self._connections.get(engagement_id, set()))


# 싱글턴
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Outbox 폴링 워커
# ---------------------------------------------------------------------------

WEBSOCKET_EVENTS = frozenset({
    "NodeStateChanged",
    "GateTriggered",
    "GateApproved",
    "GateRejected",
    "AgentRunStarted",
    "AgentRunCompleted",
    "AgentRunFailed",
    "BudgetWarning70",
    "BudgetWarning90",
    "BudgetExceeded",
    "CircuitBreakerOpened",
    "CircuitBreakerHalfOpen",
    "CircuitBreakerClosed",
    "NodeSuspendedByBudget",
    "NodeSuspendedByCircuitBreaker",
    "NodeKilledBySystem",
    "EscalationCreated",
    "SystemShutdownInitiated",
    "NodeOutputToken",
})

POLL_INTERVAL = 5.0  # seconds


async def broadcast_token(node_id: str, token: str, sequence: int) -> None:
    """
    LLM 토큰을 WebSocket을 통해 직접 브로드캐스트.
    Outbox 우회 — 지연 없는 실시간 전송.
    """
    from datetime import datetime, timezone
    await manager.broadcast({
        "type": "NodeOutputToken",
        "node_id": node_id,
        "token": token,
        "sequence": sequence,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


class OutboxWebSocketWorker:
    """
    Outbox 테이블에서 destination='WEBSOCKET' 메시지 폴링 후
    ConnectionManager를 통해 브로드캐스트.
    """

    def __init__(self, db: "DatabaseAdapter") -> None:
        self._db = db
        self._last_id: int = 0
        self._running = False

    async def run(self) -> None:
        self._running = True
        # 시작 시 마지막 processed id 로드 (재시작 후 중복 방지)
        row = await self._db.fetchone(
            "SELECT MAX(id) AS max_id FROM outbox WHERE destination='WEBSOCKET'"
        )
        if row and row["max_id"]:
            self._last_id = row["max_id"]

        logger.info("ws_outbox_worker_started last_id=%s", self._last_id)
        while self._running:
            try:
                await self._poll()
            except Exception as exc:
                logger.error("ws_outbox_worker_error error=%s", str(exc), exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        rows = await self._db.fetchall(
            """SELECT id, event_type, payload, engagement_id
               FROM outbox
               WHERE destination='WEBSOCKET'
                 AND status IN ('PENDING', 'DELIVERED')
                 AND id > ?
               ORDER BY id
               LIMIT 100""",
            (self._last_id,),
        )
        if not rows:
            return

        for row in rows:
            event_type = row["event_type"]
            engagement_id = row["engagement_id"]

            if not engagement_id:
                # engagement_id 없으면 모든 활성 engagement에 브로드캐스트
                for eid in manager.active_engagements():
                    await self._deliver(eid, event_type, row)
            elif manager.connection_count(engagement_id) > 0:
                await self._deliver(engagement_id, event_type, row)

            self._last_id = row["id"]

        # 처리 완료 마킹 (DELIVERED)
        if rows:
            max_id = rows[-1]["id"]
            await self._db.execute(
                """UPDATE outbox SET status='DELIVERED', processed_at=?
                   WHERE destination='WEBSOCKET' AND id <= ? AND status='PENDING'""",
                (_now(), max_id),
            )

    async def _deliver(
        self, engagement_id: str, event_type: str, row: dict
    ) -> None:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except json.JSONDecodeError:
            payload = {"raw": row["payload"]}

        message = {
            "type": event_type,
            "engagement_id": engagement_id,
            "payload": payload,
            "timestamp": _now(),
        }
        await manager.broadcast(engagement_id, message)
        logger.debug("ws_event_broadcast event_type=%s engagement_id=%s",
                     event_type, engagement_id)


# ---------------------------------------------------------------------------
# WebSocket 엔드포인트 팩토리
# ---------------------------------------------------------------------------

def register_websocket_routes(app, get_db_func, get_current_user_func) -> None:
    """
    FastAPI app에 WebSocket 라우트 등록.
    server.py에서 호출:
        from api.websocket import register_websocket_routes
        register_websocket_routes(app, get_db, get_current_user)
    """

    @app.websocket("/ws/stream")
    async def global_stream(websocket: WebSocket):
        """
        전역 시스템 스트림 — 인증 없이 연결 가능.
        ping/pong으로 연결 상태 확인 + 시스템 이벤트(SystemShutdownInitiated 등) 수신.
        """
        await websocket.accept()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=25.0)
                    if data == "ping":
                        await websocket.send_text("pong")
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping", "timestamp": _now()})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    @app.websocket("/ws/engagements/{engagement_id}/stream")
    async def engagement_stream(
        websocket: WebSocket,
        engagement_id: str,
    ):
        """
        인게이지먼트 실시간 이벤트 스트림.
        연결 후 즉시 현재 요약 스냅샷 전송, 이후 Outbox 이벤트 실시간 수신.
        """
        db = get_db_func()

        # 연결 수락 + 등록
        await manager.connect(websocket, engagement_id)

        try:
            # 연결 즉시 현재 상태 스냅샷 전송
            snapshot = await _build_snapshot(db, engagement_id)
            await websocket.send_json({
                "type": "Snapshot",
                "engagement_id": engagement_id,
                "payload": snapshot,
                "timestamp": _now(),
            })

            # 클라이언트 ping 수신 루프 (연결 유지)
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=30.0
                    )
                    # ping/pong
                    if data == "ping":
                        await websocket.send_text("pong")
                except asyncio.TimeoutError:
                    # 30초마다 서버 ping 전송
                    await websocket.send_json({"type": "ping", "timestamp": _now()})

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("ws_connection_error engagement_id=%s error=%s",
                         engagement_id, str(exc))
        finally:
            manager.disconnect(websocket, engagement_id)


async def _build_snapshot(db: "DatabaseAdapter", engagement_id: str) -> dict:
    """연결 시 전송할 현재 상태 스냅샷."""
    # 인게이지먼트 기본 정보
    engagement = await db.fetchone(
        "SELECT id, name, status, priority FROM engagements WHERE id=?",
        (engagement_id,),
    )
    if not engagement:
        return {"error": "engagement_not_found"}

    # 프로젝트별 노드 상태 집계
    node_summary = await db.fetchall(
        """SELECT p.id AS project_id, p.name AS project_name,
                  n.state, COUNT(*) AS cnt
           FROM projects p
           JOIN dags d ON d.project_id = p.id
           JOIN nodes n ON n.dag_id = d.id
           WHERE p.engagement_id = ?
           GROUP BY p.id, p.name, n.state""",
        (engagement_id,),
    )

    # 활성 에스컬레이션
    escalations = await db.fetchall(
        """SELECT e.id, e.node_id, e.description AS reason, e.created_at
           FROM escalations e
           JOIN nodes n ON n.id = e.node_id
           JOIN dags d ON d.id = n.dag_id
           JOIN projects p ON p.id = d.project_id
           WHERE p.engagement_id = ? AND e.resolved_at IS NULL""",
        (engagement_id,),
    )

    # 상태별 집계
    state_counts: dict[str, int] = {}
    projects_map: dict[str, dict] = {}
    for row in node_summary:
        pid = row["project_id"]
        if pid not in projects_map:
            projects_map[pid] = {"id": pid, "name": row["project_name"], "states": {}}
        projects_map[pid]["states"][row["state"]] = row["cnt"]
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + row["cnt"]

    return {
        "engagement": dict(engagement),
        "state_counts": state_counts,
        "projects": list(projects_map.values()),
        "active_escalations": [dict(r) for r in escalations],
        "snapshot_at": _now(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
