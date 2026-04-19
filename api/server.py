"""
api/server.py
FastAPI 앱 + API 라우터 + 의존성 주입.
인증: Bearer JWT (Authorization 헤더).
에러 형식: {"error": {"code": "...", "message": "...", "details": {}}}
낙관적 잠금 충돌: 409 + {"current_version": N}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Header, Request, status
from fastapi.responses import JSONResponse

from engine.db.adapter import DatabaseAdapter, create_adapter
from engine.db.migrations.runner import MigrationRunner
from engine.lifecycle.shutdown import ShutdownManager
from engine.lifecycle.startup import StartupRecovery
from engine.lifecycle.watchdog import run_watchdog
from engine.observability.logger import configure_logging, get_logger
from engine.security.rbac import RBAC, Permission, PermissionDeniedError

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 앱 싱글턴 컨테이너
# ---------------------------------------------------------------------------

class AppState:
    db: DatabaseAdapter
    shutdown_manager: ShutdownManager
    episode_store: Any = None  # Phase F: habits.py 및 기타 엔드포인트에서 접근
    dynamic_dag: Any = None    # Phase F-2: 런타임 노드 주입 (DynamicDAGExtension)


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 수명 관리."""
    configure_logging(env=os.environ.get("ENV", "production"))
    from engine.observability.tracing import setup_tracing
    setup_tracing()
    db_url = os.environ.get("DATABASE_URL", "sqlite:///platform.db")
    state.db = create_adapter(db_url)

    # 마이그레이션
    runner = MigrationRunner(state.db)
    applied = await runner.run_pending()
    if applied:
        logger.info("migrations_applied_on_startup versions=%s", applied)

    # Startup Recovery
    recovery = StartupRecovery(state.db)
    await recovery.run()

    # 초기 admin 계정 자동 생성 (최초 실행 시)
    existing = await state.db.fetchone("SELECT id FROM users LIMIT 1")
    if not existing:
        admin_id = str(uuid.uuid4())
        admin_pw = os.environ.get("V9_ADMIN_PASSWORD", secrets.token_urlsafe(24))
        pw_hash = hashlib.sha256(admin_pw.encode()).hexdigest()
        now_str = _now()
        await state.db.execute(
            """INSERT INTO users (id,email,password_hash,name,role,is_active,
               failed_login_attempts,version,created_at,updated_at)
               VALUES (?,?,?,?,?,1,0,0,?,?)""",
            (admin_id, "admin@platform.local", pw_hash,
             "관리자", "ADMIN", now_str, now_str),
        )
        logger.info("default_admin_created email=%s", "admin@platform.local")

    # ShutdownManager
    state.shutdown_manager = ShutdownManager(state.db)
    state.shutdown_manager.setup_signal_handlers()

    # WebSocket Outbox 워커 시작
    global _ws_worker
    _ws_worker = OutboxWebSocketWorker(state.db)
    import asyncio as _asyncio
    _asyncio.create_task(_ws_worker.run())

    # DAGAdvancer + Skill Executor 연결
    global _dag_advancer
    _dag_advancer = None
    try:
        from engine.core.dag_advancer import DAGAdvancer
        from engine.ai.context_assembler import ContextAssembler
        from engine.ai.model_adapter import (
            ModelAdapter, AnthropicPlaintextKeyProvider, OAuthProvider, ModelID
        )
        from engine.skills.executor import create_skill_executor
        from engine.memory.episode_store import EpisodeStore

        assembler = ContextAssembler()
        episode_store = EpisodeStore(state.db)
        state.episode_store = episode_store  # habits.py 등에서 getattr로 접근
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        oauth_config = os.environ.get("ANTHROPIC_OAUTH_CONFIG", "")

        creds = None
        auth_method = None

        if api_key:
            creds = AnthropicPlaintextKeyProvider(api_key)
            auth_method = "api_key"
        elif oauth_config:
            # OAuth: DB의 암호화된 config 또는 환경변수에서 직접 로드
            creds = OAuthProvider(oauth_config_encrypted=oauth_config, token_expires_at=None)
            auth_method = "oauth"
        else:
            # DB에서 provider_credentials 조회
            cred_row = await state.db.fetchone(
                "SELECT key_encrypted, oauth_config_encrypted, token_expires_at "
                "FROM provider_credentials WHERE provider='anthropic' AND is_active=1 LIMIT 1"
            )
            if cred_row:
                if cred_row["oauth_config_encrypted"]:
                    creds = OAuthProvider(
                        oauth_config_encrypted=cred_row["oauth_config_encrypted"],
                        token_expires_at=cred_row["token_expires_at"],
                    )
                    auth_method = "oauth_db"
                elif cred_row["key_encrypted"]:
                    from engine.ai.model_adapter import AnthropicAPIKeyProvider
                    creds = AnthropicAPIKeyProvider(cred_row["key_encrypted"])
                    auth_method = "api_key_encrypted"

        async def _supervised_run(advancer: DAGAdvancer, label: str) -> None:
            """DAGAdvancer 자동 재시작 supervisor. Exponential backoff 적용."""
            restart_count = 0
            while True:
                try:
                    await advancer.run()
                    break  # CancelledError 없이 정상 종료 시 루프 탈출
                except _asyncio.CancelledError:
                    raise
                except Exception as exc:
                    restart_count += 1
                    # 5→10→20→40→60 상한 (exponential backoff, 최대 60초)
                    wait = min(60, 5 * (2 ** (restart_count - 1)))
                    logger.critical(
                        "dag_advancer_crashed label=%s restart=%d wait=%ds error=%s",
                        label, restart_count, wait, exc, exc_info=True,
                    )
                    await _asyncio.sleep(wait)

        if creds:
            adapter = ModelAdapter(creds)
            executor_fn = await create_skill_executor(state.db, assembler, adapter, episode_store=episode_store)
            _dag_advancer = DAGAdvancer(state.db, executor_fn)
            _dag_task = _asyncio.create_task(_supervised_run(_dag_advancer, "oauth"))
            logger.info("dag_advancer_started auth=%s", auth_method)
        else:
            # Fallback: Claude Code CLI 프록시 (Pro/Max 구독 사용)
            import shutil
            from engine.ai.model_adapter import CLIProxyAdapter
            cli_path = shutil.which("claude")
            if cli_path:
                # DB에서 등록된 CLI 계정 로드 (priority 순)
                cli_rows = []
                try:
                    cli_rows = await state.db.fetchall(
                        "SELECT name, tier, config_dir FROM cli_accounts "
                        "WHERE is_active=1 ORDER BY priority ASC, created_at ASC"
                    )
                except Exception:
                    pass  # 테이블 없으면 무시 (마이그레이션 전)

                if cli_rows and len(cli_rows) >= 2:
                    # 멀티 계정 — AccountRouter로 모델 기반 라우팅
                    from engine.ai.account_router import AccountRouter, AccountStats
                    account_list = []
                    for row in cli_rows:
                        acc_adapter = CLIProxyAdapter(cli_path, config_dir=row["config_dir"])
                        account_list.append(AccountStats(
                            name=row["name"], tier=row["tier"], adapter=acc_adapter,
                        ))
                    adapter = AccountRouter(account_list, db=state.db)
                    logger.info(
                        "dag_advancer_started auth=multi_account accounts=%d",
                        len(account_list),
                    )
                elif cli_rows:
                    # 단일 계정
                    first = cli_rows[0]
                    adapter = CLIProxyAdapter(cli_path, config_dir=first["config_dir"])
                    logger.info(
                        "dag_advancer_started auth=cli_proxy account=%s tier=%s",
                        first["name"], first["tier"],
                    )
                else:
                    # 등록 계정 없음 → 현재 로그인된 계정(기본 ~/.claude) 사용
                    adapter = CLIProxyAdapter(cli_path)
                    logger.info("dag_advancer_started auth=cli_proxy cli=%s", cli_path)

                executor_fn = await create_skill_executor(state.db, assembler, adapter, episode_store=episode_store)
                _dag_advancer = DAGAdvancer(state.db, executor_fn)
                _dag_task = _asyncio.create_task(_supervised_run(_dag_advancer, "cli_proxy"))
            else:
                logger.warning("dag_advancer_skipped reason=no_credentials_and_no_cli")
    except Exception as exc:
        logger.warning("dag_advancer_init_failed error=%s", str(exc))

    # Phase F-2: DynamicDAGExtension — 런타임 노드 주입 (dag_advancer 있을 때만)
    state.dynamic_dag = None
    if _dag_advancer is not None:
        try:
            from engine.core.dynamic_dag import DynamicDAGExtension
            state.dynamic_dag = DynamicDAGExtension(
                db=state.db,
                enqueue_fn=_dag_advancer.enqueue,
            )
            logger.info("dynamic_dag_extension_started")
        except Exception as dde_exc:
            logger.warning("dynamic_dag_init_failed error=%s", str(dde_exc))

    # RUNNING 상태 DAG 자동 재개 (서버 재시작 시 중단된 작업 복구)
    if _dag_advancer:
        try:
            running_dags = await state.db.fetchall(
                "SELECT id FROM dags WHERE status='RUNNING'"
            )
            for dag_row in running_dags:
                await _dag_advancer.enqueue(dag_row["id"])
            if running_dags:
                logger.info("dag_auto_resumed count=%d", len(running_dags))
        except Exception as resume_exc:
            logger.warning("dag_auto_resume_failed error=%s", str(resume_exc))

    # Zombie node watchdog — 런타임 중 stuck IN_PROGRESS 노드 주기 감지/리셋
    # + SUSPENDED 일시오류 재개. advancer 넘겨서 복원 후 즉시 enqueue 되게 함.
    _asyncio.create_task(run_watchdog(state.db, dag_advancer=_dag_advancer))

    # Workspace Deploy 워커 시작 (BUILD 완료 → 자동 배포)
    # [v8+] ai_adapter 전달 — AI 빌드 수정 활성화
    global _deploy_worker
    from engine.workspace.auto_deploy import WorkspaceDeployWorker
    _deploy_ai = adapter if creds or (locals().get('adapter') and hasattr(adapter, 'call')) else None
    _deploy_worker = WorkspaceDeployWorker(state.db, ai_adapter=_deploy_ai)
    _asyncio.create_task(_deploy_worker.run())

    # 소급 검증 스케줄 (새 harness 규칙을 기존 COMPLETED 산출물에 적용)
    try:
        from engine.skills.qa.retroactive import schedule_retroactive_check
        _asyncio.create_task(schedule_retroactive_check(state.db))
        logger.info("retroactive_validation_scheduled")
    except Exception as _retro_exc:
        logger.warning("retroactive_schedule_failed error=%s", str(_retro_exc))

    logger.info("app_started db_url=%s", db_url.split("///")[0])
    yield

    # Workspace Deploy 워커 중지
    if _deploy_worker:
        await _deploy_worker.stop()

    # DAGAdvancer 중지
    if _dag_advancer:
        await _dag_advancer.stop()

    # WebSocket 워커 중지
    if _ws_worker:
        await _ws_worker.stop()

    # 앱 종료
    await state.shutdown_manager.shutdown()
    await state.db.close()
    logger.info("app_stopped")


app = FastAPI(
    title="AI SI 매뉴팩처링 플랫폼 v10",
    version="9.0.0",
    lifespan=lifespan,
)

# 대시보드 라우터 + WebSocket 등록 (get_db/get_current_user 정의 후 호출)
from frontend.router import register_dashboard_routes
from api.websocket import register_websocket_routes, OutboxWebSocketWorker

# WebSocket Outbox 워커 (lifespan 외부에서 태스크로 관리)
_ws_worker: OutboxWebSocketWorker | None = None
_dag_advancer = None  # DAGAdvancer 인스턴스
_deploy_worker = None  # WorkspaceDeployWorker 인스턴스


# ---------------------------------------------------------------------------
# 공통 에러 핸들러
# ---------------------------------------------------------------------------


from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
import time

# GZip 압축
app.add_middleware(GZipMiddleware, minimum_size=1024)

# CORS 명시적 설정  
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("V9_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    logger.info("%s %s → %d (%.3fs)", request.method, request.url.path, response.status_code, elapsed)
    return response


@app.exception_handler(PermissionDeniedError)
async def permission_denied_handler(request: Request, exc: PermissionDeniedError):
    return JSONResponse(
        status_code=403,
        content={"error": {"code": "PERMISSION_DENIED", "message": str(exc), "details": {}}},
    )


@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception path=%s error=%s", request.url.path, str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "내부 서버 오류", "details": {}}},
    )


# ---------------------------------------------------------------------------
# 의존성: DB + 인증
# ---------------------------------------------------------------------------

def get_db() -> DatabaseAdapter:
    return state.db


async def get_current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    db: DatabaseAdapter = Depends(get_db),
) -> dict:
    """
    JWT Bearer 토큰 검증 → 사용자 정보 반환.
    미구현 세션 검증: token_hash 비교.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 필요")

    token = authorization.removeprefix("Bearer ").strip()
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    row = await db.fetchone(
        """SELECT s.user_id, s.expires_at, s.is_revoked,
                  u.email, u.role, u.name, u.is_active
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token_hash=?""",
        (token_hash,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")
    if row["is_revoked"]:
        raise HTTPException(status_code=401, detail="만료된 토큰")
    if row["expires_at"] < _now():
        raise HTTPException(status_code=401, detail="토큰 만료")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    return dict(row)


# 대시보드 + WebSocket 라우터 등록 (get_db, get_current_user 정의 완료 후)
register_dashboard_routes(app, get_db, get_current_user)
register_websocket_routes(app, get_db, get_current_user)

# Habit Tracker 라우터 등록 (Phase F 성능 검증용)
from api.routes.habits import router as habits_router
app.include_router(habits_router)


# ---------------------------------------------------------------------------
# 헬스 체크
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 인증 API
# ---------------------------------------------------------------------------

@app.post("/api/v1/auth/login")
async def login(body: dict, db: DatabaseAdapter = Depends(get_db)):
    """이메일 + 비밀번호 → Bearer 토큰 발급."""
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="이메일과 비밀번호를 입력하세요")

    user = await db.fetchone(
        "SELECT id, password_hash, role, name, is_active, failed_login_attempts, locked_until "
        "FROM users WHERE email=?", (email,)
    )
    if not user:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 잘못됐습니다")

    # 잠금 확인
    if user["locked_until"] and user["locked_until"] > _now():
        raise HTTPException(status_code=401, detail="계정이 잠겼습니다. 잠시 후 다시 시도하세요")
    if not user["is_active"]:
        raise HTTPException(status_code=401, detail="비활성화된 계정입니다")

    if hashlib.sha256(password.encode()).hexdigest() != user["password_hash"]:
        # 실패 횟수 증가
        fails = (user["failed_login_attempts"] or 0) + 1
        locked = None
        if fails >= 5:
            locked = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        await db.execute(
            "UPDATE users SET failed_login_attempts=?, locked_until=?, updated_at=? WHERE id=?",
            (fails, locked, _now(), user["id"]),
        )
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 잘못됐습니다")

    # 로그인 성공 → 실패 횟수 초기화 + 세션 생성
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    sid = str(uuid.uuid4())
    now_str = _now()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    await db.execute(
        "UPDATE users SET failed_login_attempts=0, locked_until=NULL, last_login_at=?, updated_at=? WHERE id=?",
        (now_str, now_str, user["id"]),
    )
    await db.execute(
        """INSERT INTO sessions (id, user_id, token_hash, expires_at, is_revoked, created_at, last_used_at)
           VALUES (?,?,?,?,0,?,?)""",
        (sid, user["id"], token_hash, expires, now_str, now_str),
    )
    return {
        "token": token,
        "user": {"id": user["id"], "name": user["name"], "role": user["role"], "email": email},
        "expires_at": expires,
    }


@app.post("/api/v1/auth/logout")
async def logout(
    current_user: dict = Depends(get_current_user),
    authorization: Optional[str] = Header(default=None),
    db: DatabaseAdapter = Depends(get_db),
):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await db.execute(
            "UPDATE sessions SET is_revoked=1, last_used_at=? WHERE token_hash=?",
            (_now(), token_hash),
        )
    return {"ok": True}


@app.get("/health")
async def health(db: DatabaseAdapter = Depends(get_db)):
    row = await db.fetchone("SELECT 1 AS ok")
    return {"status": "ok", "db": bool(row)}


@app.get("/api/v1/status")
async def system_status(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    nodes = await db.fetchone(
        """SELECT
           SUM(CASE WHEN state='IN_PROGRESS' THEN 1 ELSE 0 END) AS active,
           SUM(CASE WHEN state='NEEDS_HUMAN' THEN 1 ELSE 0 END) AS needs_human,
           SUM(CASE WHEN state='SUSPENDED'   THEN 1 ELSE 0 END) AS suspended
           FROM nodes"""
    )
    return {
        "shutting_down": state.shutdown_manager.is_shutting_down,
        "active_agents": nodes["active"] or 0,
        "needs_human": nodes["needs_human"] or 0,
        "suspended": nodes["suspended"] or 0,
    }


# ---------------------------------------------------------------------------
# 인게이지먼트 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/engagements")
async def list_engagements(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT id, name, client_name, status, priority, created_at FROM engagements ORDER BY priority, created_at DESC"
    )
    return {"engagements": [dict(r) for r in rows]}


@app.post("/api/v1/engagements", status_code=201)
async def create_engagement(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    eid = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """INSERT INTO engagements
           (id, name, client_name, status, global_context, priority, created_by, created_at, updated_at)
           VALUES (?, ?, ?, 'INTAKE', ?, ?, ?, ?, ?)""",
        (
            eid,
            body.get("name", ""),
            body.get("client_name", ""),
            json.dumps(body.get("global_context", {})),
            body.get("priority", 3),
            current_user["user_id"],
            now, now,
        ),
    )
    return {"id": eid, "created_at": now}


@app.get("/api/v1/engagements/{engagement_id}")
async def get_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    row = await db.fetchone("SELECT * FROM engagements WHERE id=?", (engagement_id,))
    if not row:
        raise HTTPException(status_code=404, detail="인게이지먼트 없음")
    return dict(row)


@app.get("/api/v1/engagements/{engagement_id}/summary")
async def engagement_summary(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    row = await db.fetchone(
        "SELECT * FROM v_engagement_summary WHERE engagement_id=?", (engagement_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="인게이지먼트 없음")
    return dict(row)


# ---------------------------------------------------------------------------
# 노드 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/projects/{project_id}/nodes")
async def list_nodes(
    project_id: str,
    state_filter: Optional[str] = None,
    phase_filter: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    query = "SELECT * FROM nodes WHERE project_id=?"
    params: list[Any] = [project_id]
    if state_filter:
        query += " AND state=?"
        params.append(state_filter)
    if phase_filter:
        query += " AND phase=?"
        params.append(phase_filter)
    query += " ORDER BY phase, priority"
    rows = await db.fetchall(query, tuple(params))
    return {"nodes": [dict(r) for r in rows]}


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/approve")
async def approve_gate(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """Gate 노드 수동 승인 (AWAITING_APPROVAL → COMPLETED)."""
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    node = await db.fetchone(
        "SELECT state, version FROM nodes WHERE id=? AND project_id=?",
        (node_id, project_id),
    )
    if not node:
        raise HTTPException(status_code=404, detail="노드 없음")
    if node["state"] != "AWAITING_APPROVAL":
        raise HTTPException(status_code=409, detail=f"현재 상태: {node['state']}")

    affected = await db.execute(
        """UPDATE nodes SET state='COMPLETED', completed_at=?, updated_at=?, version=version+1
           WHERE id=? AND version=?""",
        (_now(), _now(), node_id, node["version"]),
    )
    if affected == 0:
        raise HTTPException(status_code=409, detail="동시 수정 충돌")

    # DESIGN→BUILD GATE 승인 시: 산출물 기반 환경변수 자동 추출
    try:
        gate_name = await db.fetchone("SELECT name FROM nodes WHERE id=?", (node_id,))
        if gate_name and "DESIGN" in (gate_name["name"] or ""):
            from engine.core.env_config_generator import extract_env_from_artifacts
            eng = await db.fetchone(
                "SELECT engagement_id FROM projects WHERE id=?", (project_id,)
            )
            if eng:
                new_keys = await extract_env_from_artifacts(
                    db, project_id, eng["engagement_id"]
                )
                if new_keys:
                    logger.info("gate_approve_env_extracted project=%s keys=%d", project_id[:8], len(new_keys))
    except Exception as _e:
        logger.debug("gate_approve_env_extract_skip error=%s", _e)

    # DAG 재큐 — 하위 BLOCKED 노드를 READY로 전환
    if _dag_advancer:
        dag_row = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
        if dag_row:
            await _dag_advancer.enqueue(dag_row["dag_id"])

    return {"status": "approved"}


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/feedback")
async def gate_feedback(
    project_id: str,
    node_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """Gate 수정 요청 — 피드백 기반으로 관련 BUILD 노드를 INVALID 처리.

    AI가 피드백을 분석하여 영향 노드 특정 → cascade.
    GATE는 NOT_STARTED로 리셋 (BUILD 재완료 후 다시 AWAITING_APPROVAL).
    """
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    feedback = (body or {}).get("feedback", "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="피드백 내용 필요")

    now = _now()

    # GATE → NOT_STARTED 리셋
    gate = await db.fetchone(
        "SELECT state, dag_id, version FROM nodes WHERE id=? AND project_id=?",
        (node_id, project_id),
    )
    if not gate:
        raise HTTPException(status_code=404, detail="노드 없음")

    await db.execute(
        "UPDATE nodes SET state='NOT_STARTED', updated_at=?, version=version+1 WHERE id=?",
        (now, node_id),
    )

    # BUILD phase TASK 노드 중 피드백 키워드와 관련된 노드 INVALID 처리
    build_tasks = await db.fetchall(
        """SELECT id, name FROM nodes
           WHERE project_id=? AND phase='BUILD' AND node_type='TASK'
             AND state='COMPLETED'""",
        (project_id,),
    )

    # 피드백에서 페이지/기능명 매칭하여 관련 노드 특정
    feedback_lower = feedback.lower()
    invalidated = []
    for task in build_tasks:
        task_name_lower = task["name"].lower()
        # 노드 이름에서 핵심어 추출 (괄호 안 그룹명 등)
        if any(kw in feedback_lower for kw in task_name_lower.split()):
            await db.execute(
                """UPDATE nodes SET state='INVALID',
                   failure_reasons=json_insert(COALESCE(failure_reasons, '[]'), '$[#]',
                     json_object('attempt', 0, 'reason', ?, 'at', ?)),
                   updated_at=? WHERE id=?""",
                (f"사용자 피드백: {feedback[:200]}", now, now, task["id"]),
            )
            # QA도 리셋
            qa_row = await db.fetchone(
                "SELECT qa_pair_node_id FROM nodes WHERE id=?", (task["id"],)
            )
            if qa_row and qa_row["qa_pair_node_id"]:
                await db.execute(
                    "UPDATE nodes SET state='NOT_STARTED', retry_count=0, updated_at=? WHERE id=?",
                    (now, qa_row["qa_pair_node_id"]),
                )
            invalidated.append(task["name"])
            logger.info("gate_feedback_invalidated task=%s feedback=%s", task["name"], feedback[:100])

    # 매칭 안 되면 프론트엔드 관련 노드 전체 INVALID (안전 폴백)
    if not invalidated:
        for task in build_tasks:
            if "프론트엔드" in task["name"]:
                await db.execute(
                    """UPDATE nodes SET state='INVALID',
                       failure_reasons=json_insert(COALESCE(failure_reasons, '[]'), '$[#]',
                         json_object('attempt', 0, 'reason', ?, 'at', ?)),
                       updated_at=? WHERE id=?""",
                    (f"사용자 피드백: {feedback[:200]}", now, now, task["id"]),
                )
                qa_row = await db.fetchone(
                    "SELECT qa_pair_node_id FROM nodes WHERE id=?", (task["id"],)
                )
                if qa_row and qa_row["qa_pair_node_id"]:
                    await db.execute(
                        "UPDATE nodes SET state='NOT_STARTED', retry_count=0, updated_at=? WHERE id=?",
                        (now, qa_row["qa_pair_node_id"]),
                    )
                invalidated.append(task["name"])

    # DAG 재큐
    if _dag_advancer and gate.get("dag_id"):
        await _dag_advancer.enqueue(gate["dag_id"])

    logger.info(
        "gate_feedback_processed project=%s feedback=%s invalidated=%s",
        project_id, feedback[:100], invalidated,
    )
    return {"status": "feedback_accepted", "invalidated": invalidated}


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/skip")
async def skip_node(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """노드 건너뛰기 — FAILED/BLOCKED → SKIPPED."""
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    now = _now()
    node = await db.fetchone(
        "SELECT state, version, qa_pair_node_id, dag_id FROM nodes WHERE id=? AND project_id=?",
        (node_id, project_id),
    )
    if not node:
        raise HTTPException(status_code=404, detail="노드 없음")
    if node["state"] not in ("FAILED", "BLOCKED", "NOT_STARTED"):
        raise HTTPException(status_code=409, detail=f"현재 상태 {node['state']}에서 건너뛰기 불가")

    # TASK → SKIPPED
    await db.execute(
        "UPDATE nodes SET state='SKIPPED', updated_at=?, version=version+1 WHERE id=?",
        (now, node_id),
    )
    # QA 쌍도 SKIPPED
    if node["qa_pair_node_id"]:
        await db.execute(
            "UPDATE nodes SET state='SKIPPED', updated_at=?, version=version+1 WHERE id=?",
            (now, node["qa_pair_node_id"]),
        )
    # DAG 재큐 (하위 노드 BLOCKED 해제)
    if _dag_advancer and node["dag_id"]:
        await _dag_advancer.enqueue(node["dag_id"])

    return {"status": "skipped"}


# ---------------------------------------------------------------------------
# 수정 정책 검증
# ---------------------------------------------------------------------------

# Phase 순서 (인덱스가 클수록 후반)
_PHASE_ORDER = {"DEFINE": 0, "DESIGN": 1, "BUILD": 2, "VERIFY": 3, "DELIVER": 4}


async def _check_revision_policy(db, project_id: str, node_id: str, force: bool = False) -> dict:
    """수정 요청의 정책 검증.

    Returns:
        {
            "blocked": bool,       # True면 수정 차단
            "reason": str,         # 차단/경고 사유
            "warning": str | None, # 차단은 아니지만 주의 필요
            "cascade_count": int,  # 영향받는 하위 노드 수
        }
    """
    # 단일 쿼리로 노드 정보 + 프로젝트 최신 phase를 한 번에 가져옴 (DB 잠금 최소화)
    row = await db.fetchone(
        """SELECT n.node_type, n.phase, n.name,
                  (SELECT MAX(CASE n2.phase
                     WHEN 'DELIVER' THEN 5 WHEN 'VERIFY' THEN 4 WHEN 'BUILD' THEN 3
                     WHEN 'DESIGN' THEN 2 WHEN 'DEFINE' THEN 1 ELSE 0 END)
                   FROM nodes n2 JOIN dags d2 ON d2.id=n2.dag_id
                   WHERE d2.project_id=? AND n2.state='COMPLETED' AND n2.node_type='TASK'
                  ) as latest_phase_idx
           FROM nodes n JOIN dags d ON d.id=n.dag_id
           WHERE n.id=? AND d.project_id=?""",
        (project_id, node_id, project_id),
    )
    if not row:
        return {"blocked": True, "reason": "노드를 찾을 수 없습니다.", "warning": None, "cascade_count": 0}

    node_type = row["node_type"]
    phase = row["phase"]
    name = row["name"]
    latest_phase_idx = row["latest_phase_idx"] or 0

    # ── 1. 절대 차단: 수정 불가 노드 유형 ──
    if node_type == "QA":
        return {"blocked": True, "reason": "QA 산출물은 수정할 수 없습니다. QA는 자동 검증 결과이며, 수정하면 검증 의미가 사라집니다.", "warning": None, "cascade_count": 0}

    if node_type == "GATE":
        return {"blocked": True, "reason": "GATE 노드는 수정할 수 없습니다. 승인/반려만 가능합니다.", "warning": None, "cascade_count": 0}

    # ── 2. 절대 차단: VERIFY 단계 테스트 결과 ──
    if phase == "VERIFY":
        return {"blocked": True, "reason": f"VERIFY 단계 산출물({name})은 수정할 수 없습니다. 테스트 결과를 직접 수정하면 검증이 무효화됩니다. BUILD/DESIGN을 수정하세요.", "warning": None, "cascade_count": 0}

    # ── 3. 페이지 조립 수정 차단 (렌더러 산출물) ──
    if "페이지 조립" in name:
        return {"blocked": True, "reason": "페이지 조립 산출물은 직접 수정할 수 없습니다. 렌더러가 자동 생성하는 결과물이며, 수정해도 재조립 시 덮어씌워집니다. 페이지 레시피/컴포넌트 라이브러리/디자인 토큰을 수정하세요.", "warning": None, "cascade_count": 0}

    # ── 4. Phase 차이 기반 경고/차단 ──
    _PHASE_IDX = {"DEFINE": 1, "DESIGN": 2, "BUILD": 3, "VERIFY": 4, "DELIVER": 5}
    current_idx = _PHASE_IDX.get(phase, 0)
    phase_gap = latest_phase_idx - current_idx
    _PHASE_NAMES = {1: "DEFINE", 2: "DESIGN", 3: "BUILD", 4: "VERIFY", 5: "DELIVER"}
    latest_name = _PHASE_NAMES.get(latest_phase_idx, "?")

    if phase_gap >= 2 and not force:
        return {
            "blocked": True,
            "reason": f"⚠️ {phase} 단계 산출물({name})을 수정하면 하위 노드들이 재실행됩니다. 현재 {latest_name} 단계까지 진행됨. 정말 수정하려면 force: true를 사용하세요.",
            "warning": None,
            "cascade_count": 0,
        }

    warning = None
    if phase_gap >= 1:
        warning = f"{phase} 단계 산출물 수정 — 하위 노드 재실행 발생. (현재 {latest_name}까지 진행됨)"

    return {"blocked": False, "reason": "", "warning": warning, "cascade_count": 0}


# ---------------------------------------------------------------------------
# revise_artifact 헬퍼 함수들
# ---------------------------------------------------------------------------

async def _revise_call_ai(adapter, art: dict, current_content: str, request_text: str) -> tuple[str, str, int]:
    """AI에게 수정 요청 → (new_content, complexity, revise_max_tokens) 반환."""
    import re as _re_rev, json as _json_rev

    # 복잡도 판단 (haiku)
    pre_judge_prompt = f"""수정 요청의 성격을 판단하세요.

수정 요청: {request_text}

JSON으로만 응답: {{"complexity": "simple 또는 moderate 또는 complex"}}
- simple: 오타, 수치, 날짜, 문구 변경, 예시 추가/삭제
- moderate: 기능 추가/삭제, 항목 추가, 내용 보강
- complex: 구조 변경, 프로세스 재설계, 대규모 변경"""

    try:
        pre_resp = await adapter.call(
            model="claude-haiku-4-5-20251001",
            prompt=pre_judge_prompt, max_tokens=50, temperature=0,
        )
        pre_match = _re_rev.search(r'\{[^}]+\}', pre_resp.content)
        complexity = _json_rev.loads(pre_match.group())["complexity"] if pre_match else "moderate"
    except Exception:
        complexity = "moderate"

    MODEL_MAP_REVISE = {
        "simple": ("claude-haiku-4-5-20251001", 4000),
        "moderate": ("claude-sonnet-4-6", 8000),
        "complex": ("claude-opus-4-6", 16000),
    }
    revise_model, revise_max_tokens = MODEL_MAP_REVISE.get(complexity, MODEL_MAP_REVISE["moderate"])

    # HTML 산출물이면 HTML만 추출
    source_content = current_content
    if art["artifact_type"] == "html" and "<!DOCTYPE" in current_content:
        match = _re_rev.search(r'(<!DOCTYPE[\s\S]*</html>)', current_content, _re_rev.IGNORECASE)
        if match:
            source_content = match.group(1)

    revise_prompt = f"""아래 산출물을 수정 요청에 따라 수정하세요.

## 수정 요청
{request_text}

## 현재 산출물
{source_content[:15000]}

## 지시
- 수정 요청 사항만 반영하고, 나머지는 그대로 유지하세요.
- 산출물 전체를 다시 출력하세요 (수정된 부분만이 아니라 전체).
- TODO, TBD, 미정 단어 사용 금지.
- 원본 형식(마크다운 또는 HTML)을 유지하세요.
- 도구 호출(function_calls, invoke 등)을 하지 마세요. 산출물 텍스트만 출력하세요.
- HTML 산출물이면 <!DOCTYPE html>부터 </html>까지 완전한 HTML만 출력하세요."""

    return revise_prompt, revise_model, revise_max_tokens


async def _analyze_revision_impact(adapter, request_text: str, diff_text: str) -> dict:
    """변경 영향도 AI 판단 (6단계 분류)."""
    import re, json as _json
    impact_analysis = {"type": "context_change", "strategy": "full", "reason": request_text, "skip_cascade": False}
    if not diff_text:
        return impact_analysis
    try:
        judge_prompt = f"""아래는 프로젝트 산출물의 변경 diff입니다. 이 변경의 성격을 판단하세요.

수정 요청: {request_text}

```diff
{diff_text[:2000]}
```

아래 JSON 형식으로만 응답하세요:
{{"type": "타입", "reason": "판단 이유 한 줄", "strategy": "full 또는 diff", "skip_cascade": true 또는 false, "needs_upstream": true 또는 false}}

타입 판단 기준 (6단계):
[변경]
- context_change: 비즈니스 로직, 구조, 프로세스 흐름 변경 → strategy: "full", skip_cascade: false
- minor_fix: 오타, 날짜, 수치 보정, 포맷 변경 → strategy: "diff", skip_cascade: true
[추가]
- add_major: 핵심 기능 추가, 사용자 역할 추가, 데이터 모델에 영향 있는 추가 → strategy: "full", skip_cascade: false
- add_minor: 부가 설명 보충, 예시 추가, 참고사항 추가 → strategy: "diff", skip_cascade: true
[삭제]
- delete_major: 핵심 기능 삭제, 모듈 제거, 역할 제거 등 하위 의존 있는 삭제 → strategy: "full", skip_cascade: false
- delete_minor: 불필요한 예시 제거, 중복 내용 정리, 주석 삭제 → strategy: "diff", skip_cascade: true

needs_upstream 판단: 이 변경이 상위 산출물(PRD, 요구사항 등)에도 반영되어야 하면 true. 예: 기능 추가/삭제는 PRD에도 반영 필요 → true. 단순 디자인 변경은 → false."""

        judge_resp = await adapter.call(
            model="claude-sonnet-4-6",
            prompt=judge_prompt, max_tokens=200, temperature=0,
        )
        json_match = re.search(r'\{[^}]+\}', judge_resp.content)
        if json_match:
            impact_analysis = _json.loads(json_match.group())
    except Exception as judge_err:
        logger.warning("revise_impact_analysis_failed error=%s", str(judge_err))
    return impact_analysis


async def _cascade_invalidate(db, node_id: str, art: dict, new_ver: int,
                              new_content: str, diff_lines: list, diff_text: str,
                              strategy: str, downstream_out: list) -> int:
    """cascade INVALID 처리 + delta 생성. invalidated 수 반환."""
    import json as _json
    invalidated = 0
    now = _now()
    async with db.begin_immediate():
        downstream = await db.fetchall(
            """SELECT n.id, n.state FROM edges e
               JOIN nodes n ON n.id = e.to_node_id
               WHERE e.from_node_id = ? AND e.is_active = 1
                 AND n.state IN ('COMPLETED', 'IN_PROGRESS', 'READY')""",
            (node_id,),
        )
        for d in downstream:
            await db.execute(
                "UPDATE nodes SET state='INVALID', updated_at=?, version=version+1 WHERE id=?",
                (now, d["id"]),
            )
            invalidated += 1
        if invalidated:
            delta_content = diff_text if strategy == "diff" else f"[전체 문서 — 맥락 변경]\n{new_content[:8000]}"
            await db.execute(
                """INSERT INTO deltas (id, artifact_id, from_version_num, to_version_num,
                   diff_strategy, delta_content, delta_size_bytes, is_empty,
                   impacted_node_ids, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (str(uuid.uuid4()), art["id"], new_ver - 1, new_ver,
                 "unified_diff" if strategy == "diff" else "full_document",
                 delta_content, len(delta_content),
                 _json.dumps([d["id"] for d in downstream]), now),
            )
        downstream_out.extend(downstream)
    return invalidated


async def _revise_upstream(db, adapter, node_id: str, request_text: str,
                           invalidated_ref: list) -> list[str]:
    """상류 산출물 자동 수정. 수정된 노드 이름 목록 반환."""
    upstream_revised = []

    async def _collect_upstream(nid, depth=0, visited=None):
        if visited is None:
            visited = set()
        if depth >= 3 or nid in visited:
            return []
        visited.add(nid)
        parents = await db.fetchall(
            """SELECT n.id, n.name FROM edges e
               JOIN nodes n ON n.id = e.from_node_id
               WHERE e.to_node_id = ? AND e.is_active = 1
                 AND n.node_type = 'TASK' AND n.state = 'COMPLETED'""",
            (nid,),
        )
        result = []
        for p in parents:
            result.extend(await _collect_upstream(p["id"], depth + 1, visited))
            if p["id"] not in {r["id"] for r in result}:
                result.append(p)
        return result

    upstream = await _collect_upstream(node_id)
    for u in upstream:
        u_art = await db.fetchone("SELECT id FROM artifacts WHERE node_id=?", (u["id"],))
        if not u_art:
            continue
        u_ver = await db.fetchone(
            "SELECT storage_path AS content FROM artifact_versions WHERE artifact_id=? ORDER BY version_num DESC LIMIT 1",
            (u_art["id"],),
        )
        if not u_ver or not u_ver["content"]:
            continue

        try:
            u_source = u_ver["content"][:10000]
            u_prompt = f"""아래 산출물의 하위 문서에서 변경이 발생했습니다. 수정이 필요한 부분만 알려주세요.

## 하위 변경 내용
{request_text}

## 현재 산출물 ({u['name']})
{u_source}

## 지시 — 부분 수정 패치 형식으로 응답하세요:
1. 먼저 수정이 필요한지 판단하세요. 필요 없으면 "NO_CHANGE" 한 단어만 출력.
2. 수정이 필요하면 아래 형식으로 응답:

PATCH_START
FIND: (기존 텍스트 — 변경할 부분만, 앞뒤 1줄 포함)
REPLACE: (수정된 텍스트)
PATCH_END

여러 곳이면 PATCH_START/PATCH_END를 반복.
산출물 전체를 다시 출력하지 마세요. 변경 부분만."""

            u_resp = await adapter.call(
                model="claude-sonnet-4-6",
                prompt=u_prompt, max_tokens=2000,
            )
            u_result = u_resp.content.strip()

            if u_result == "NO_CHANGE" or u_result.startswith("NO_CHANGE"):
                continue

            u_new = u_ver["content"]
            if "PATCH_START" in u_result:
                import re as _re_patch
                patches = _re_patch.findall(
                    r'PATCH_START\s*\nFIND:\s*(.*?)\nREPLACE:\s*(.*?)\nPATCH_END',
                    u_result, _re_patch.DOTALL
                )
                for find_text, replace_text in patches:
                    find_text = find_text.strip()
                    replace_text = replace_text.strip()
                    if find_text and find_text in u_new:
                        u_new = u_new.replace(find_text, replace_text, 1)
            else:
                u_new = u_result
                if "<!DOCTYPE" in u_new:
                    import re as _re3
                    m = _re3.search(r'(<!DOCTYPE[\s\S]*</html>)', u_new, _re3.IGNORECASE)
                    if m: u_new = m.group(1)

            if u_new == u_ver["content"]:
                continue

            import hashlib as _hl2
            u_now = _now()
            u_art_full = await db.fetchone("SELECT current_version FROM artifacts WHERE id=?", (u_art["id"],))
            u_new_ver = (u_art_full["current_version"] or 0) + 1
            await db.execute("UPDATE artifacts SET current_version=?, updated_at=? WHERE id=?",
                             (u_new_ver, u_now, u_art["id"]))
            await db.execute(
                "INSERT INTO artifact_versions (id, artifact_id, version_num, storage_path, content_hash, size_bytes, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), u_art["id"], u_new_ver, u_new,
                 _hl2.sha256(u_new.encode()).hexdigest(), len(u_new), "auto-upstream", u_now))
            upstream_revised.append(u["name"])
            qa_row = await db.fetchone("SELECT qa_pair_node_id FROM nodes WHERE id=?", (u["id"],))
            if qa_row and qa_row["qa_pair_node_id"]:
                await db.execute(
                    "UPDATE nodes SET state='INVALID', updated_at=? WHERE id=? AND state='COMPLETED'",
                    (u_now, qa_row["qa_pair_node_id"]),
                )
            u_downstream = await db.fetchall(
                """SELECT n.id FROM edges e JOIN nodes n ON n.id=e.to_node_id
                   WHERE e.from_node_id=? AND e.is_active=1 AND n.state='COMPLETED'
                     AND n.id != ?""",
                (u["id"], node_id),
            )
            for ud in u_downstream:
                await db.execute(
                    "UPDATE nodes SET state='INVALID', updated_at=? WHERE id=?",
                    (u_now, ud["id"]),
                )
                invalidated_ref[0] += 1
            logger.info("upstream_auto_revised node=%s version=%d", u["name"], u_new_ver)
        except Exception as ue:
            logger.warning("upstream_revise_failed node=%s error=%s", u["name"], str(ue))

    return upstream_revised


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/revise")
async def revise_artifact(
    project_id: str,
    node_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """자연어 수정 요청 → AI가 산출물 수정 → 새 버전 저장."""
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    request_text = body.get("request", "").strip()
    if not request_text:
        raise HTTPException(status_code=400, detail="수정 요청 내용을 입력하세요")
    force = body.get("force", False)

    # ── 수정 정책 검증 ──────────────────────────────────────────────
    policy = await _check_revision_policy(db, project_id, node_id, force)
    if policy["blocked"]:
        raise HTTPException(status_code=403, detail=policy["reason"])
    # policy["warning"]이 있으면 응답에 포함 (차단은 안 함)

    # 현재 산출물 로드
    art = await db.fetchone(
        "SELECT id, artifact_type, current_version FROM artifacts WHERE node_id=? AND project_id=?",
        (node_id, project_id),
    )
    if not art:
        raise HTTPException(status_code=404, detail="산출물 없음")

    await db.execute(
        "UPDATE nodes SET state='IN_PROGRESS', updated_at=? WHERE id=? AND state='COMPLETED'",
        (_now(), node_id),
    )

    ver = await db.fetchone(
        "SELECT storage_path AS content FROM artifact_versions WHERE artifact_id=? ORDER BY version_num DESC LIMIT 1",
        (art["id"],),
    )
    if not ver:
        raise HTTPException(status_code=404, detail="산출물 버전 없음")
    current_content = ver["content"]

    # 예산 검증
    from engine.core.budget_enforcer import BudgetEnforcer, InputBudgetExceededError
    _node_meta = await db.fetchone(
        "SELECT n.phase, p.engagement_id FROM nodes n JOIN dags d ON d.id=n.dag_id JOIN projects p ON p.id=d.project_id WHERE n.id=?",
        (node_id,),
    )

    # AI 수정 호출 준비
    import shutil
    from engine.ai.model_adapter import CLIProxyAdapter
    cli = shutil.which("claude")
    if not cli:
        raise HTTPException(status_code=503, detail="CLI 프록시 없음")
    adapter = CLIProxyAdapter(cli)

    revise_prompt, revise_model, revise_max_tokens = await _revise_call_ai(
        adapter, art, current_content, request_text,
    )

    # L1 예산 검사
    if _node_meta and _node_meta.get("engagement_id"):
        try:
            budget_max = await BudgetEnforcer(db).pre_call_check(
                node_id=node_id,
                engagement_id=_node_meta["engagement_id"],
                phase=_node_meta.get("phase", "BUILD"),
                prompt=revise_prompt,
            )
            revise_max_tokens = min(revise_max_tokens, budget_max)
        except InputBudgetExceededError as e:
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', updated_at=? WHERE id=? AND state='IN_PROGRESS'",
                (_now(), node_id),
            )
            raise HTTPException(status_code=429, detail=str(e))

    resp = await adapter.call(model=revise_model, prompt=revise_prompt, max_tokens=revise_max_tokens)
    new_content = resp.content

    # HTML 추출
    if art["artifact_type"] == "html" and "<!DOCTYPE" in new_content:
        import re as _re
        match = _re.search(r'(<!DOCTYPE[\s\S]*</html>)', new_content, _re.IGNORECASE)
        if match:
            new_content = match.group(1)

    # 새 버전 저장
    import hashlib as _hl
    now = _now()
    new_ver = (art["current_version"] or 0) + 1
    content_hash = _hl.sha256(new_content.encode("utf-8")).hexdigest()
    await db.execute(
        "UPDATE artifacts SET current_version=?, artifact_type=?, updated_at=? WHERE id=?",
        (new_ver, art["artifact_type"], now, art["id"]),
    )
    await db.execute(
        "INSERT INTO artifact_versions (id, artifact_id, version_num, "
        "storage_path, content_hash, size_bytes, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), art["id"], new_ver, new_content,
         content_hash, len(new_content), current_user["user_id"], now),
    )

    await _composition_revise_hook(db, node_id, project_id, new_content)

    # 영향도 분석
    import difflib
    diff_lines = list(difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        lineterm="",
    ))
    diff_text = "\n".join(diff_lines[:500])
    impact_analysis = await _analyze_revision_impact(adapter, request_text, diff_text)

    # cascade 처리
    invalidated = 0
    strategy = impact_analysis.get("strategy", "full")
    skip_cascade = impact_analysis.get("skip_cascade", False)
    downstream = []

    if not skip_cascade and diff_lines:
        invalidated = await _cascade_invalidate(
            db, node_id, art, new_ver, new_content, diff_lines, diff_text, strategy, downstream,
        )
        if invalidated and _dag_advancer:
            dag_row = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
            if dag_row:
                dag_status = await db.fetchone("SELECT status FROM dags WHERE id=?", (dag_row["dag_id"],))
                if dag_status and dag_status["status"] == "COMPLETED":
                    await db.execute(
                        "UPDATE dags SET status='RUNNING', updated_at=? WHERE id=?",
                        (now, dag_row["dag_id"]),
                    )

    # 수정 완료 → COMPLETED 전환
    node_state = await db.fetchone("SELECT state, qa_pair_node_id FROM nodes WHERE id=?", (node_id,))
    if node_state and node_state["state"] in ("FAILED", "IN_PROGRESS"):
        await db.execute(
            "UPDATE nodes SET state='COMPLETED', completed_at=?, retry_count=0, "
            "failure_reasons='[]', updated_at=?, version=version+1 WHERE id=?",
            (now, now, node_id),
        )
        if node_state["qa_pair_node_id"]:
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, retry_count=0, "
                "updated_at=?, version=version+1 WHERE id=?",
                (now, now, node_state["qa_pair_node_id"]),
            )
        if _dag_advancer:
            dag_row2 = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
            if dag_row2:
                dag_status2 = await db.fetchone("SELECT status FROM dags WHERE id=?", (dag_row2["dag_id"],))
                if dag_status2 and dag_status2["status"] == "COMPLETED":
                    await db.execute(
                        "UPDATE dags SET status='RUNNING', updated_at=? WHERE id=?",
                        (now, dag_row2["dag_id"]),
                    )
                await _dag_advancer.enqueue(dag_row2["dag_id"])

    logger.info("artifact_revised node_id=%s version=%d invalidated=%d strategy=%s impact=%s user=%s",
                node_id, new_ver, invalidated, strategy,
                impact_analysis.get("type", "?"), current_user["user_id"])

    # 상류 산출물 자동 수정
    upstream_revised = []
    if impact_analysis.get("needs_upstream", False):
        invalidated_ref = [invalidated]
        upstream_revised = await _revise_upstream(db, adapter, node_id, request_text, invalidated_ref)
        invalidated = invalidated_ref[0]

    result = {"version": new_ver, "invalidated": invalidated, "content": new_content,
              "impact": impact_analysis.get("type", "unknown"), "reason": impact_analysis.get("reason", ""),
              "upstream_revised": upstream_revised}
    if policy.get("warning"):
        result["warning"] = policy["warning"]
    return result


@app.get("/api/v1/projects/{project_id}/nodes/{node_id}/artifact/view")
async def view_artifact_html(
    project_id: str,
    node_id: str,
    db: DatabaseAdapter = Depends(get_db),
):
    """모든 산출물을 HTML로 브라우저 렌더링. 마크다운은 서버에서 HTML 변환."""
    from fastapi.responses import HTMLResponse
    import re as _rev

    art = await db.fetchone(
        "SELECT id, artifact_type FROM artifacts WHERE node_id=? AND project_id=?",
        (node_id, project_id),
    )
    if not art:
        return HTMLResponse("<h1>산출물 없음</h1>", status_code=404)

    node = await db.fetchone("SELECT name FROM nodes WHERE id=?", (node_id,))
    node_name = node["name"] if node else "산출물"

    ver = await db.fetchone(
        "SELECT storage_path AS content, version_num FROM artifact_versions WHERE artifact_id=? ORDER BY version_num DESC LIMIT 1",
        (art["id"],),
    )
    if not ver or not ver["content"]:
        return HTMLResponse("<h1>산출물 버전 없음</h1>", status_code=404)

    content = ver["content"]

    # AI tool_calls / function_calls 태그 제거
    content = _rev.sub(
        r'<(?:antml:)?(?:function_calls|invoke|tool_calls)[\s\S]*?</(?:antml:)?(?:function_calls|invoke|tool_calls)>',
        '', content,
    )

    # HTML 산출물 → 그대로 반환
    if art["artifact_type"] == "html" or "<!DOCTYPE" in content:
        match = _rev.search(r'(<!DOCTYPE[\s\S]*</html>)', content, _rev.IGNORECASE)
        if match:
            content = match.group(1)
        return HTMLResponse(content)

    # JSON 산출물 → 포맷팅 + 구문 강조
    if art["artifact_type"] == "json":
        return HTMLResponse(_json_to_html(content, node_name, ver["version_num"]))

    # 마크다운 산출물 → HTML 변환 + base.css 감싸기
    from pathlib import Path
    base_css_path = Path(__file__).parent.parent / "engine" / "skills" / "specs" / "_html_templates" / "base.css"
    base_css = base_css_path.read_text("utf-8") if base_css_path.exists() else ""

    version = ver["version_num"]
    html_body = _markdown_to_html(content)

    page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{node_name} (v{version})</title>
<style>
{base_css}
</style>
</head>
<body>
<div class="toolbar">
  <a class="back" href="javascript:history.back()">← 돌아가기</a>
  <span style="flex:1"></span>
  <button class="dl-btn" onclick="window.print()">PDF 저장</button>
</div>
<div class="meta"><span>{node_name}</span><span>버전: v{version}</span></div>
{html_body}
</body>
</html>"""
    return HTMLResponse(page)


def _json_to_html(content: str, node_name: str, version: int) -> str:
    """JSON 산출물을 포맷팅된 HTML로 렌더링 (구문 강조 포함)."""
    import json as _json
    import html as _html

    # JSON 파싱 → pretty print
    try:
        parsed = _json.loads(content)
        formatted = _json.dumps(parsed, indent=2, ensure_ascii=False)
    except (_json.JSONDecodeError, ValueError):
        formatted = content

    # 구문 강조 (CSS 클래스 기반)
    import re as _re
    escaped = _html.escape(formatted)
    # JSON 키: "key":
    escaped = _re.sub(
        r'&quot;([^&]+?)&quot;(\s*:)',
        r'<span class="jk">&quot;\1&quot;</span>\2',
        escaped,
    )
    # JSON 문자열 값: "value"
    escaped = _re.sub(
        r':\s*&quot;([^&]*?)&quot;',
        r': <span class="js">&quot;\1&quot;</span>',
        escaped,
    )
    # 숫자
    escaped = _re.sub(
        r':\s*(-?\d+\.?\d*)',
        r': <span class="jn">\1</span>',
        escaped,
    )
    # boolean / null
    escaped = _re.sub(
        r':\s*(true|false|null)',
        r': <span class="jb">\1</span>',
        escaped,
    )

    # 아이템 수 요약
    try:
        parsed = _json.loads(content)
        if isinstance(parsed, list):
            summary = f"{len(parsed)}개 항목"
        elif isinstance(parsed, dict):
            summary = f"{len(parsed)}개 키"
        else:
            summary = ""
    except Exception:
        summary = "파싱 실패 — raw 데이터 표시"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(node_name)} (v{version})</title>
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0d1117; color:#c9d1d9; font-family:'Pretendard Variable',system-ui,sans-serif; }}
.toolbar {{ display:flex; align-items:center; padding:12px 24px; background:#161b22; border-bottom:1px solid #30363d; position:sticky; top:0; z-index:10; }}
.toolbar .back {{ color:#58a6ff; text-decoration:none; font-size:14px; }}
.toolbar .back:hover {{ text-decoration:underline; }}
.dl-btn {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; padding:6px 16px; border-radius:6px; cursor:pointer; font-size:13px; }}
.dl-btn:hover {{ background:#30363d; }}
.meta {{ padding:16px 24px 8px; display:flex; justify-content:space-between; align-items:center; }}
.meta .name {{ font-size:18px; font-weight:700; color:#f0f6fc; }}
.meta .ver {{ font-size:13px; color:#8b949e; }}
.meta .summary {{ font-size:13px; color:#8b949e; background:#1c2128; padding:4px 10px; border-radius:4px; }}
pre {{ margin:16px 24px; padding:20px; background:#0d1117; border:1px solid #30363d; border-radius:8px; overflow-x:auto; font-size:13px; line-height:1.6; font-family:'SF Mono','Fira Code',monospace; }}
.jk {{ color:#7ee787; }}  /* JSON key — green */
.js {{ color:#a5d6ff; }}  /* JSON string — blue */
.jn {{ color:#f2cc60; }}  /* JSON number — yellow */
.jb {{ color:#ff7b72; }}  /* JSON bool/null — red */
@media print {{ .toolbar {{ display:none; }} pre {{ border:1px solid #ddd; background:#fff; color:#24292f; }} .jk {{ color:#116329; }} .js {{ color:#0550ae; }} .jn {{ color:#953800; }} .jb {{ color:#cf222e; }} body {{ background:#fff; }} }}
</style>
</head>
<body>
<div class="toolbar">
  <a class="back" href="javascript:history.back()">← 돌아가기</a>
  <span style="flex:1"></span>
  <button class="dl-btn" onclick="window.print()">PDF 저장</button>
</div>
<div class="meta">
  <div><span class="name">{_html.escape(node_name)}</span> <span class="ver">버전: v{version}</span></div>
  <span class="summary">{summary}</span>
</div>
<pre><code>{escaped}</code></pre>
</body>
</html>"""


def _markdown_to_html(md: str) -> str:
    """마크다운을 HTML로 변환 (외부 라이브러리 없이)."""
    import re

    # HTML 주석 제거 (SELF_CHECK 등)
    md = re.sub(r'<!--[\s\S]*?-->', '', md)

    # 코드블록 보호
    code_blocks = []
    def _save_code(m):
        code_blocks.append((m.group(1) or 'text', m.group(2)))
        return f'%%CODE_{len(code_blocks)-1}%%'
    md = re.sub(r'```(\w*)\n([\s\S]*?)```', _save_code, md)

    # 인라인 코드 보호
    inline_codes = []
    def _save_inline(m):
        inline_codes.append(m.group(1))
        return f'%%INLINE_{len(inline_codes)-1}%%'
    md = re.sub(r'`([^`]+)`', _save_inline, md)

    # 이스케이프
    md = md.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # 헤딩
    md = re.sub(r'^#### (.+)$', r'<h4>\1</h4>', md, flags=re.MULTILINE)
    md = re.sub(r'^### (.+)$', r'<h3>\1</h3>', md, flags=re.MULTILINE)
    md = re.sub(r'^## (.+)$', r'<h2>\1</h2>', md, flags=re.MULTILINE)
    md = re.sub(r'^# (.+)$', r'<h1>\1</h1>', md, flags=re.MULTILINE)

    # 테이블
    def _table(m):
        header, sep, body = m.group(1), m.group(2), m.group(3)
        ths = ''.join(f'<th>{c.strip()}</th>' for c in header.split('|') if c.strip())
        rows = ''
        for row in body.strip().split('\n'):
            tds = ''.join(f'<td>{c.strip()}</td>' for c in row.split('|') if c.strip())
            rows += f'<tr>{tds}</tr>'
        return f'<div class="section"><table><thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table></div>'
    md = re.sub(r'^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)+)', _table, md, flags=re.MULTILINE)

    # 볼드/이탈릭
    md = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', md)
    md = re.sub(r'\*(.+?)\*', r'<em>\1</em>', md)

    # 목록
    md = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', md, flags=re.MULTILINE)
    md = re.sub(r'^[-*] (.+)$', r'<li>\1</li>', md, flags=re.MULTILINE)
    md = re.sub(r'((?:<li>.+</li>\n?)+)', r'<ul>\1</ul>', md)

    # 수평선
    md = re.sub(r'^---+$', '<hr>', md, flags=re.MULTILINE)

    # 단락
    md = re.sub(r'\n\n+', '</p><p>', md)
    md = '<p>' + md + '</p>'
    md = md.replace('<p></p>', '').replace('<p><h', '<h').replace('</h1></p>', '</h1>')
    md = md.replace('</h2></p>', '</h2>').replace('</h3></p>', '</h3>').replace('</h4></p>', '</h4>')
    md = md.replace('<p><ul>', '<ul>').replace('</ul></p>', '</ul>')
    md = md.replace('<p><div', '<div').replace('</div></p>', '</div>')
    md = md.replace('<p><hr></p>', '<hr>').replace('<p><hr>', '<hr>')

    # 코드블록 복원
    for i, (lang, code) in enumerate(code_blocks):
        escaped = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        md = md.replace(f'%%CODE_{i}%%',
            f'<pre><code class="lang-{lang}">{escaped}</code></pre>')

    # 인라인 코드 복원
    for i, code in enumerate(inline_codes):
        escaped = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        md = md.replace(f'%%INLINE_{i}%%', f'<code>{escaped}</code>')

    return md


@app.get("/api/v1/nodes/{node_id}/failure-reasons")
async def get_failure_reasons(
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """노드 실패 사유 조회."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    import json as _json
    row = await db.fetchone(
        "SELECT failure_reasons, retry_count, max_retries FROM nodes WHERE id=?",
        (node_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="노드 없음")
    try:
        reasons = _json.loads(row["failure_reasons"] or "[]")
    except (ValueError, TypeError):
        reasons = []
    return {"reasons": reasons, "retry_count": row["retry_count"], "max_retries": row["max_retries"]}


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/retry")
async def retry_node(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """스마트 재시도: TASK 상태에 따라 전체 재실행 또는 QA만 재실행 자동 판단.

    - TASK FAILED/SUSPENDED → TASK 전체 재실행
    - TASK COMPLETED/SUSPENDED + QA FAILED → QA만 재실행 (산출물 보존)
    """
    from engine.core.validation_gateway import ValidationGateway
    RBAC.require(current_user["role"], Permission.RETRY_NODE)

    node = await db.fetchone(
        "SELECT id, state, node_type, qa_pair_node_id, task_pair_node_id FROM nodes WHERE id=? AND project_id=?",
        (node_id, project_id),
    )
    if not node:
        raise HTTPException(status_code=404, detail="노드 없음")

    now = _now()
    dag_id = None

    # 케이스 1: TASK 노드이고 산출물이 존재 → QA만 재실행
    if node["node_type"] == "TASK" and node["state"] in ("SUSPENDED", "COMPLETED"):
        # 산출물 존재 확인
        art = await db.fetchone(
            "SELECT id FROM artifacts WHERE node_id=? AND (SELECT COUNT(*) FROM artifact_versions WHERE artifact_id=artifacts.id) > 0",
            (node_id,),
        )
        qa_node = await db.fetchone(
            "SELECT id, state FROM nodes WHERE (task_pair_node_id=? OR qa_pair_node_id=?) AND node_type='QA'",
            (node_id, node_id),
        )
        if art and qa_node and qa_node["state"] in ("FAILED", "BLOCKED", "NOT_STARTED", "SUSPENDED"):
            # QA stall_count >= 2: 같은 산출물로 QA 반복 FAIL → TASK도 재실행
            qa_stall = await db.fetchone("SELECT stall_count FROM nodes WHERE id=?", (qa_node["id"],))
            if qa_stall and (qa_stall["stall_count"] or 0) >= 2:
                logger.info("smart_retry_full_due_to_qa_stall task=%s qa_stall=%d", node_id[:8], qa_stall["stall_count"])
                await db.execute("UPDATE nodes SET stall_count=0 WHERE id=?", (node_id,))
                await db.execute("DELETE FROM agent_token_usage WHERE node_id=?", (node_id,))
                gw = ValidationGateway(db)
                success = await gw.c9_manual_retry(node_id, current_user["role"])
                if not success:
                    raise HTTPException(status_code=409, detail="재실행 불가 상태")
                if _dag_advancer:
                    dag_row = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
                    if dag_row:
                        await _dag_advancer.enqueue(dag_row["dag_id"])
                return {"status": "retried", "mode": "full_qa_stall"}

            # TASK → COMPLETED 유지 + stall_count/토큰 리셋, QA만 리셋
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', retry_count=0, stall_count=0, updated_at=? WHERE id=? AND state != 'COMPLETED'",
                (now, node_id),
            )
            await db.execute("DELETE FROM agent_token_usage WHERE node_id=?", (node_id,))
            await db.execute(
                "UPDATE nodes SET state='NOT_STARTED', retry_count=0, failure_reasons='[]', description=NULL, updated_at=? WHERE id=?",
                (now, qa_node["id"]),
            )
            logger.info("smart_retry_qa_only task=%s qa=%s", node_id[:8], qa_node["id"][:8])
            dag_row = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
            if dag_row and _dag_advancer:
                await _dag_advancer.enqueue(dag_row["dag_id"])
            return {"status": "retried", "mode": "qa_only"}

    # 케이스 2: 일반 재시도 (TASK 전체 재실행)
    # stall_count 리셋 + 토큰 사용량 리셋 (사용자 수동 재시도이므로 카운터 초기화)
    await db.execute("UPDATE nodes SET stall_count=0 WHERE id=?", (node_id,))
    await db.execute("DELETE FROM agent_token_usage WHERE node_id=?", (node_id,))
    gw = ValidationGateway(db)
    success = await gw.c9_manual_retry(node_id, current_user["role"])
    if not success:
        raise HTTPException(status_code=409, detail="재실행 불가 상태")
    if _dag_advancer:
        dag_row = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
        if dag_row:
            await _dag_advancer.enqueue(dag_row["dag_id"])
    return {"status": "retried", "mode": "full"}


@app.post("/api/v1/projects/{project_id}/deduplicate-nodes")
async def deduplicate_nodes(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """중복 DAG 노드 정리. 동일 (name, phase, node_type) → 최신 1개만 유지."""
    RBAC.require(current_user["role"], Permission.MANAGE_PROJECT)
    dag_row = await db.fetchone(
        "SELECT id FROM dags WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not dag_row:
        raise HTTPException(status_code=404, detail="DAG 없음")
    from engine.intake.processor import deduplicate_dag_nodes
    result = await deduplicate_dag_nodes(db, dag_row["id"])
    return result


# ---------------------------------------------------------------------------
# 인테이크 API
# ---------------------------------------------------------------------------

@app.post("/api/v1/intake/public", status_code=201)
async def submit_intake_public(
    body: dict,
    db: DatabaseAdapter = Depends(get_db),
):
    """인증 불필요 — 클라이언트 대면 공개 접수 폼 전용."""
    sid = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """INSERT INTO intake_submissions
           (id, form_version, raw_json, status, created_at, updated_at)
           VALUES (?, 'v1-public', ?, 'RECEIVED', ?, ?)""",
        (sid, json.dumps(body, ensure_ascii=False), now, now),
    )
    return {"submission_id": sid, "status": "RECEIVED"}


@app.post("/api/v1/intake/submit", status_code=201)
async def submit_intake(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.MANAGE_INTAKE)
    sid = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """INSERT INTO intake_submissions
           (id, raw_json, status, created_at, updated_at)
           VALUES (?, ?, 'RECEIVED', ?, ?)""",
        (sid, json.dumps(body, ensure_ascii=False), now, now),
    )
    return {"submission_id": sid, "status": "RECEIVED"}


@app.get("/api/v1/intake/submissions")
async def list_intake_submissions(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.MANAGE_INTAKE)
    rows = await db.fetchall(
        "SELECT id, raw_json, status, engagement_id, created_at, updated_at "
        "FROM intake_submissions ORDER BY created_at DESC LIMIT 100"
    )
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["parsed"] = json.loads(d["raw_json"])
        except Exception:
            d["parsed"] = {}
        result.append(d)
    return result


@app.post("/api/v1/intake/submissions/{submission_id}/convert", status_code=201)
async def convert_intake_to_engagement(
    submission_id: str,
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """
    인테이크 접수 → Engagement 전환 (프로젝트 착수).

    v9 변경:
      ① body에서 name / client_name 우선 읽기 (모달 입력값)
      ② raw_json 폴백: snake_case(신규 폼) → camelCase(구 폼) 순
      ③ 멱등성: CONVERTING/CONVERTED 상태이면 기존 engagement_id 반환
      ④ 경쟁조건 방지: WHERE status IN ('RECEIVED','VALID') 원자적 UPDATE로 이중 전환 차단
      ⑤ IntakeProcessor를 통해 DAG + 노드 자동 생성
    """
    from engine.intake.processor import IntakeProcessor, _resolve

    RBAC.require(current_user["role"], Permission.MANAGE_INTAKE)
    row = await db.fetchone(
        "SELECT id, raw_json, status, engagement_id FROM intake_submissions WHERE id=?",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="제출 없음")

    # ── 멱등성: 이미 처리됐으면 기존 결과 반환 ───────────────────────────
    if row["status"] == "CONVERTED" and row["engagement_id"]:
        return {"engagement_id": row["engagement_id"], "idempotent": True}
    if row["status"] == "CONVERTING":
        raise HTTPException(status_code=409, detail="이미 전환 처리 중입니다. 잠시 후 재시도하세요.")

    if row["status"] not in ("RECEIVED", "VALID"):
        raise HTTPException(status_code=400, detail=f"전환 불가 상태: {row['status']}")

    data = json.loads(row["raw_json"])

    # ── 이름·클라이언트: body 우선 → raw_json 폴백 ───────────────────────
    name = (body.get("name") or "").strip() or _resolve(
        data,
        "project_name",    # 신규 폼 (snake_case)
        "projectName",     # 구 폼 (camelCase)
        default=f"프로젝트 {submission_id[:8]}",
    )
    client = (body.get("client_name") or "").strip() or _resolve(
        data,
        "contact_company", # 내부 폼
        "company_name",    # 공개 폼
        "contactCompany",  # 구 폼
        default="",
    )
    priority = body.get("priority") or _resolve(data, "priority", default=3)

    now = _now()

    # ── 경쟁조건 방지: WHERE 절로 원자적 상태 전환 ───────────────────────
    affected = await db.execute(
        """UPDATE intake_submissions SET status='CONVERTING', updated_at=?
           WHERE id=? AND status IN ('RECEIVED', 'VALID')""",
        (now, submission_id),
    )
    if affected == 0:
        # 다른 요청이 먼저 전환 시작 → 현재 상태 재조회
        current = await db.fetchone(
            "SELECT status, engagement_id FROM intake_submissions WHERE id=?",
            (submission_id,),
        )
        if current and current["status"] == "CONVERTED" and current["engagement_id"]:
            return {"engagement_id": current["engagement_id"], "idempotent": True}
        raise HTTPException(status_code=409, detail="동시 전환 요청 충돌. 잠시 후 재시도하세요.")

    # ── 레퍼런스 URL 분석 (크롤링 + AI 요약) ────────────────────────────
    try:
        from engine.intake.reference_analyzer import analyze_references
        import shutil as _shutil
        _cli = _shutil.which("claude")
        _ref_adapter = None
        if _cli:
            from engine.ai.model_adapter import CLIProxyAdapter
            _ref_adapter = CLIProxyAdapter(_cli)
        ref_results = await analyze_references(data, adapter=_ref_adapter)
        if ref_results:
            data["_reference_analysis"] = ref_results
            await db.execute(
                "UPDATE intake_submissions SET raw_json=?, updated_at=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), now, submission_id),
            )
    except Exception as _ref_exc:
        logger.warning("reference_analysis_failed submission_id=%s error=%s",
                        submission_id, str(_ref_exc))

    # ── 프로젝트 Plan 자동 생성 ─────────────────────────────────────────
    try:
        from engine.intake.project_planner import generate_project_plan
        plan = await generate_project_plan(data, adapter=_ref_adapter)
        if plan:
            data["_project_plan"] = plan
            await db.execute(
                "UPDATE intake_submissions SET raw_json=?, updated_at=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), now, submission_id),
            )
    except Exception as _plan_exc:
        logger.warning("project_plan_failed submission_id=%s error=%s",
                        submission_id, str(_plan_exc))

    # ── IntakeProcessor로 Engagement + DAG + 노드 생성 ───────────────────
    # body에서 받은 name/client를 raw에 덮어써서 processor에 전달
    overridden_raw = {**data, "project_name": name, "contact_company": client, "priority": priority}

    processor = IntakeProcessor(db, current_user["user_id"])
    try:
        result = await processor._create_engagement_and_projects(submission_id, overridden_raw)

        # Phase F-4: backendChoice를 engagement/projects에 반영
        # intake 폼에서 'backendChoice' (camelCase) 또는 'backend_choice' (snake) 둘 다 허용
        _bk_raw = overridden_raw.get("backendChoice") or overridden_raw.get("backend_choice") or "sql"
        _allowed_backends = {"sql", "instantdb", "firebase", "supabase", "custom"}
        _bk_choice = _bk_raw if _bk_raw in _allowed_backends else "sql"
        try:
            await db.execute(
                "UPDATE engagements SET backend_choice=? WHERE id=?",
                (_bk_choice, result.engagement_id),
            )
            await db.execute(
                "UPDATE projects SET backend_choice=? WHERE engagement_id=?",
                (_bk_choice, result.engagement_id),
            )
            logger.info(
                "backend_choice_applied engagement=%s backend=%s",
                result.engagement_id[:8], _bk_choice,
            )
        except Exception as _bk_exc:
            logger.warning(
                "backend_choice_update_failed engagement=%s error=%s",
                result.engagement_id[:8], _bk_exc,
            )

        # CONVERTED 마킹 (processor._update_submission_status 대신 여기서 처리)
        await db.execute(
            """UPDATE intake_submissions
               SET status='CONVERTED', engagement_id=?, updated_at=? WHERE id=?""",
            (result.engagement_id, _now(), submission_id),
        )

        logger.info(
            "intake_converted",
            submission_id=submission_id,
            engagement_id=result.engagement_id,
            project_count=len(result.project_ids),
        )
        return {"engagement_id": result.engagement_id, "name": name}

    except Exception as exc:
        # 실패 시 FAILED 마킹
        await db.execute(
            "UPDATE intake_submissions SET status='FAILED', updated_at=? WHERE id=?",
            (_now(), submission_id),
        )
        logger.error(
            "intake_conversion_failed",
            submission_id=submission_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"전환 실패: {exc}")


# ---------------------------------------------------------------------------
# 인테이크 상태 변경 / 삭제 API
# ---------------------------------------------------------------------------

# 허용된 수동 상태 전이 테이블
_INTAKE_STATUS_TRANSITIONS: dict[str, list[str]] = {
    "RECEIVED":   ["VALID"],           # 검토 시작
    "VALID":      ["RECEIVED"],        # 검토 취소
    "FAILED":     ["RECEIVED"],        # 재시도
    "CONVERTING": ["RECEIVED"],        # stuck 강제 초기화
}


@app.patch("/api/v1/intake/submissions/{submission_id}/status")
async def update_intake_status(
    submission_id: str,
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """수동 상태 전이. body: { "status": "VALID" | "RECEIVED" }"""
    RBAC.require(current_user["role"], Permission.MANAGE_INTAKE)

    new_status = (body.get("status") or "").strip().upper()
    if not new_status:
        raise HTTPException(status_code=400, detail="status 필드 필요")

    row = await db.fetchone(
        "SELECT id, status FROM intake_submissions WHERE id=?",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="접수 없음")

    allowed = _INTAKE_STATUS_TRANSITIONS.get(row["status"], [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"전이 불가: {row['status']} → {new_status} (허용: {allowed})",
        )

    await db.execute(
        "UPDATE intake_submissions SET status=?, updated_at=? WHERE id=?",
        (new_status, _now(), submission_id),
    )
    logger.info("intake_status_updated submission_id=%s from=%s to=%s user=%s",
                submission_id, row["status"], new_status, current_user["user_id"])
    return {"id": submission_id, "status": new_status}


@app.delete("/api/v1/intake/submissions/{submission_id}", status_code=204)
async def delete_intake_submission(
    submission_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """접수 삭제. CONVERTED / CONVERTING 상태는 삭제 불가."""
    RBAC.require(current_user["role"], Permission.MANAGE_INTAKE)

    row = await db.fetchone(
        "SELECT id, status FROM intake_submissions WHERE id=?",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="접수 없음")
    if row["status"] in ("CONVERTED", "CONVERTING"):
        raise HTTPException(
            status_code=409,
            detail=f"'{row['status']}' 상태는 삭제할 수 없습니다.",
        )

    await db.execute(
        "DELETE FROM intake_submissions WHERE id=?",
        (submission_id,),
    )
    logger.info("intake_deleted submission_id=%s user=%s",
                submission_id, current_user["user_id"])
    # 204 No Content


# ---------------------------------------------------------------------------
# 인게이지먼트 확장 API (PATCH / DELETE / pause / resume / force-close)
# ---------------------------------------------------------------------------

@app.patch("/api/v1/engagements/{engagement_id}")
async def update_engagement(
    engagement_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    row = await db.fetchone("SELECT version FROM engagements WHERE id=?", (engagement_id,))
    if not row:
        raise HTTPException(status_code=404, detail="인게이지먼트 없음")
    allowed = {"name", "client_name", "priority", "deadline", "global_context"}
    sets, vals = [], []
    for k in allowed:
        if k in body:
            sets.append(f"{k}=?")
            vals.append(json.dumps(body[k]) if k == "global_context" else body[k])
    if not sets:
        raise HTTPException(status_code=400, detail="수정할 필드 없음")
    sets.append("updated_at=?"); vals.append(_now())
    sets.append("version=version+1")
    vals += [engagement_id, row["version"]]
    affected = await db.execute(
        f"UPDATE engagements SET {', '.join(sets)} WHERE id=? AND version=?", tuple(vals)
    )
    if affected == 0:
        raise HTTPException(status_code=409, detail="동시 수정 충돌")
    return {"updated": True}


@app.delete("/api/v1/engagements/{engagement_id}", status_code=204)
async def delete_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "UPDATE engagements SET status='ARCHIVED', updated_at=? WHERE id=?",
        (_now(), engagement_id),
    )


@app.post("/api/v1/engagements/{engagement_id}/pause")
async def pause_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "UPDATE engagements SET status='PAUSED', updated_at=? WHERE id=? AND status='ACTIVE'",
        (_now(), engagement_id),
    )
    return {"status": "paused"}


@app.post("/api/v1/engagements/{engagement_id}/resume")
async def resume_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "UPDATE engagements SET status='ACTIVE', updated_at=? WHERE id=? AND status='PAUSED'",
        (_now(), engagement_id),
    )
    return {"status": "resumed"}


@app.post("/api/v1/engagements/{engagement_id}/force-close")
async def force_close_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    await db.execute(
        "UPDATE engagements SET status='FORCE_CLOSED', updated_at=? WHERE id=?",
        (_now(), engagement_id),
    )
    # 진행 중 노드 강제 중단
    await db.execute(
        """UPDATE nodes SET state='SUSPENDED', suspension_reason='SHUTDOWN_DRAIN',
           updated_at=?, version=version+1
           WHERE project_id IN (SELECT id FROM projects WHERE engagement_id=?)
             AND state='IN_PROGRESS'""",
        (_now(), engagement_id),
    )
    return {"status": "force_closed"}


@app.post("/api/v1/engagements/{engagement_id}/reset")
async def reset_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """인게이지먼트 초기화 — 모든 노드/아티팩트 삭제 후 DAG 재생성.
    ACTIVE/PAUSED/FORCE_CLOSED 상태에서만 가능."""
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    now = _now()

    eng = await db.fetchone(
        "SELECT id, status, intake_submission_id FROM engagements WHERE id=?",
        (engagement_id,),
    )
    if not eng:
        raise HTTPException(status_code=404, detail="인게이지먼트 없음")

    # 프로젝트 조회
    projects = await db.fetchall(
        "SELECT id FROM projects WHERE engagement_id=?", (engagement_id,),
    )
    project_ids = [p["id"] for p in projects]

    # DAG ID 조회
    dag_ids = []
    for pid in project_ids:
        dags = await db.fetchall("SELECT id FROM dags WHERE project_id=?", (pid,))
        dag_ids.extend(d["id"] for d in dags)

    # 산출물/노드/엣지/델타 삭제 — executescript로 FK 비활성화
    stmts = ["PRAGMA foreign_keys = OFF;"]
    for did in dag_ids:
        node_rows = await db.fetchall("SELECT id FROM nodes WHERE dag_id=?", (did,))
        for n in node_rows:
            nid = n["id"]
            for tbl in ("artifact_qa_stamps", "agent_token_usage", "agent_runs",
                        "agent_processes", "escalations", "revision_requests",
                        "node_budget_overrides"):
                col = "qa_node_id" if tbl == "artifact_qa_stamps" else "node_id"
                stmts.append(f"DELETE FROM {tbl} WHERE {col}='{nid}';")
            art_rows = await db.fetchall("SELECT id FROM artifacts WHERE node_id=?", (nid,))
            for a in art_rows:
                stmts.append(f"DELETE FROM artifact_versions WHERE artifact_id='{a['id']}';")
                stmts.append(f"DELETE FROM deltas WHERE artifact_id='{a['id']}';")
            stmts.append(f"DELETE FROM artifacts WHERE node_id='{nid}';")
        stmts.append(f"DELETE FROM edges WHERE dag_id='{did}';")
        stmts.append(f"DELETE FROM nodes WHERE dag_id='{did}';")
        stmts.append(f"DELETE FROM dags WHERE id='{did}';")
    for pid in project_ids:
        stmts.append(f"DELETE FROM artifacts WHERE project_id='{pid}';")
    stmts.append("PRAGMA foreign_keys = ON;")
    await db.executescript("\n".join(stmts))

    # 인게이지먼트 → INTAKE, 프로젝트 → INTAKE
    await db.execute(
        "UPDATE engagements SET status='INTAKE', updated_at=? WHERE id=?",
        (now, engagement_id),
    )
    for pid in project_ids:
        await db.execute(
            "UPDATE projects SET status='INTAKE', phase='DEFINE', updated_at=? WHERE id=?",
            (now, pid),
        )

    # intake_submission은 CONVERTED 유지 (DAG 재생성은 /dag/start에서 처리)

    logger.info("engagement_reset engagement_id=%s projects=%d dags=%d",
                engagement_id, len(project_ids), len(dag_ids))
    return {"status": "reset", "deleted_dags": len(dag_ids), "deleted_projects": 0}


@app.delete("/api/v1/engagements/{engagement_id}/purge", status_code=204)
async def purge_engagement(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """인게이지먼트 완전 삭제 (복구 불가) — 프로젝트/DAG/노드/아티팩트 모두 삭제."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)

    # FK 일시 비활성화 후 관련 데이터 전부 삭제
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        projects = await db.fetchall(
            "SELECT id FROM projects WHERE engagement_id=?", (engagement_id,),
        )
        project_ids = [p["id"] for p in projects]

        for pid in project_ids:
            dag_rows = await db.fetchall("SELECT id FROM dags WHERE project_id=?", (pid,))
            dag_ids = [d["id"] for d in dag_rows]

            for did in dag_ids:
                node_rows = await db.fetchall("SELECT id FROM nodes WHERE dag_id=?", (did,))
                node_ids = [n["id"] for n in node_rows]

                for nid in node_ids:
                    await db.execute("DELETE FROM artifact_qa_stamps WHERE qa_node_id=?", (nid,))
                    await db.execute("DELETE FROM agent_token_usage WHERE node_id=?", (nid,))
                    await db.execute("DELETE FROM agent_runs WHERE node_id=?", (nid,))
                    await db.execute("DELETE FROM agent_processes WHERE node_id=?", (nid,))
                    await db.execute("DELETE FROM escalations WHERE node_id=?", (nid,))
                    await db.execute("DELETE FROM revision_requests WHERE node_id=?", (nid,))
                    await db.execute("DELETE FROM node_budget_overrides WHERE node_id=?", (nid,))
                    art_rows = await db.fetchall("SELECT id FROM artifacts WHERE node_id=?", (nid,))
                    for a in art_rows:
                        await db.execute("DELETE FROM artifact_versions WHERE artifact_id=?", (a["id"],))
                        await db.execute("DELETE FROM deltas WHERE artifact_id=?", (a["id"],))
                        await db.execute("DELETE FROM artifact_qa_stamps WHERE artifact_id=?", (a["id"],))
                    await db.execute("DELETE FROM artifacts WHERE node_id=?", (nid,))

                await db.execute("DELETE FROM edges WHERE dag_id=?", (did,))
                await db.execute("DELETE FROM nodes WHERE dag_id=?", (did,))

            await db.execute("DELETE FROM artifacts WHERE project_id=?", (pid,))
            for did in dag_ids:
                await db.execute("DELETE FROM dags WHERE id=?", (did,))
            await db.execute("DELETE FROM requirements WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM cost_tracking WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM budget_limits WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM revision_requests WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM escalations WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM project_members WHERE project_id=?", (pid,))
            await db.execute("DELETE FROM projects WHERE id=?", (pid,))

        await db.execute("DELETE FROM engagement_dags WHERE engagement_id=?", (engagement_id,))
        await db.execute("DELETE FROM engagement_edges WHERE from_project_id IN (SELECT id FROM projects WHERE engagement_id=?)", (engagement_id,))
        await db.execute("DELETE FROM engagement_members WHERE engagement_id=?", (engagement_id,))
        await db.execute("DELETE FROM notification_subscriptions WHERE engagement_id=?", (engagement_id,))
        await db.execute("DELETE FROM intake_submissions WHERE engagement_id=?", (engagement_id,))
        await db.execute("DELETE FROM engagements WHERE id=?", (engagement_id,))
    finally:
        await db.execute("PRAGMA foreign_keys = ON")

    logger.info("engagement_purged engagement_id=%s", engagement_id)


# ---------------------------------------------------------------------------
# 인게이지먼트 멤버 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/engagements/{engagement_id}/members")
async def list_engagement_members(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        """SELECT em.user_id, em.role, em.joined_at, u.name, u.email
           FROM engagement_members em
           JOIN users u ON u.id = em.user_id
           WHERE em.engagement_id=?""",
        (engagement_id,),
    )
    return {"members": [dict(r) for r in rows]}


@app.post("/api/v1/engagements/{engagement_id}/members", status_code=201)
async def add_engagement_member(
    engagement_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "INSERT OR IGNORE INTO engagement_members (engagement_id, user_id, role, joined_at) VALUES (?,?,?,?)",
        (engagement_id, body["user_id"], body.get("role", "MEMBER"), _now()),
    )
    return {"joined": True}


@app.delete("/api/v1/engagements/{engagement_id}/members/{user_id}", status_code=204)
async def remove_engagement_member(
    engagement_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "DELETE FROM engagement_members WHERE engagement_id=? AND user_id=?",
        (engagement_id, user_id),
    )


# ---------------------------------------------------------------------------
# 인게이지먼트 환경변수 API
# ---------------------------------------------------------------------------

# ── 프로젝트별 환경변수 ──
@app.get("/api/v1/projects/{project_id}/env-vars")
async def list_project_env_vars(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT key, description, is_secret, created_at FROM project_env_vars WHERE scope='PROJECT' AND scope_id=?",
        (project_id,),
    )
    return {"env_vars": [dict(r) for r in rows]}


@app.get("/api/v1/projects/{project_id}/env-summary")
async def get_project_env_summary(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """프로젝트 환경변수 설정 현황 요약 (전체/설정완료/미설정 수 + 키 목록)."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    project = await db.fetchone("SELECT engagement_id FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "프로젝트 없음")
    from engine.core.env_config_generator import get_env_summary
    return await get_env_summary(db, project_id, project["engagement_id"])


@app.post("/api/v1/projects/{project_id}/env-vars/generate", status_code=201)
async def regenerate_project_env_defaults(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """프로젝트 환경변수 기본값 재생성 (기존 값은 유지, 새 키만 추가)."""
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    project = await db.fetchone(
        "SELECT engagement_id, component_type, global_context FROM projects WHERE id=?",
        (project_id,),
    )
    if not project:
        raise HTTPException(404, "프로젝트 없음")
    import json
    raw = json.loads(project["global_context"] or "{}")
    from engine.core.env_config_generator import generate_env_defaults
    keys = await generate_env_defaults(
        db, project_id, project["engagement_id"],
        project["component_type"] or "MASTER", raw, current_user["user_id"],
    )
    return {"generated_keys": keys, "count": len(keys)}


@app.post("/api/v1/projects/{project_id}/env-vars", status_code=201)
async def set_project_env_var(
    project_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    from engine.core.resource_resolver import ResourceResolver
    resolver = ResourceResolver(db)
    await resolver.set(
        scope="PROJECT", scope_id=project_id,
        key=body["key"], value=body["value"],
        created_by=current_user["user_id"],
        is_secret=body.get("is_secret", True),
        description=body.get("description", ""),
    )
    return {"set": True}


@app.delete("/api/v1/projects/{project_id}/env-vars/{key}", status_code=204)
async def delete_project_env_var(
    project_id: str,
    key: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "DELETE FROM project_env_vars WHERE scope='PROJECT' AND scope_id=? AND key=?",
        (project_id, key),
    )


# ── 인게이지먼트별 환경변수 ──
@app.get("/api/v1/engagements/{engagement_id}/env-vars")
async def list_engagement_env_vars(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT key, description, is_secret, created_at FROM project_env_vars WHERE scope='ENGAGEMENT' AND scope_id=?",
        (engagement_id,),
    )
    return {"env_vars": [dict(r) for r in rows]}


@app.post("/api/v1/engagements/{engagement_id}/env-vars", status_code=201)
async def set_engagement_env_var(
    engagement_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    from engine.core.resource_resolver import ResourceResolver
    resolver = ResourceResolver(db)
    await resolver.set(
        scope="ENGAGEMENT", scope_id=engagement_id,
        key=body["key"], value=body["value"],
        created_by=current_user["user_id"],
        is_secret=body.get("is_secret", True),
        description=body.get("description", ""),
    )
    return {"set": True}


@app.delete("/api/v1/engagements/{engagement_id}/env-vars/{key}", status_code=204)
async def delete_engagement_env_var(
    engagement_id: str,
    key: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "DELETE FROM project_env_vars WHERE scope='ENGAGEMENT' AND scope_id=? AND key=?",
        (engagement_id, key),
    )


# ---------------------------------------------------------------------------
# 인게이지먼트 프로젝트 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/engagements/{engagement_id}/projects")
async def list_engagement_projects(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT id, name, project_type, status, phase, priority, created_at FROM projects WHERE engagement_id=? ORDER BY priority, created_at",
        (engagement_id,),
    )
    return {"projects": [dict(r) for r in rows]}


@app.post("/api/v1/engagements/{engagement_id}/projects", status_code=201)
async def create_project_in_engagement(
    engagement_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    pid = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """INSERT INTO projects
           (id, name, client_name, project_type, status, global_context,
            engagement_id, priority, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'INTAKE', ?, ?, ?, ?, ?, ?)""",
        (
            pid, body.get("name", ""), body.get("client_name", ""),
            body.get("project_type", "custom"),
            json.dumps(body.get("global_context", {})),
            engagement_id, body.get("priority", 3),
            current_user["user_id"], now, now,
        ),
    )
    return {"id": pid, "created_at": now}


# ---------------------------------------------------------------------------
# DAG 시작 / 중지 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/engagements/{engagement_id}/deliverables/preview")
async def preview_deliverables(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """착수 전 산출물 목록 미리보기 — 가감 선택용."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    from engine.intake.processor import NODE_TEMPLATES

    eng = await db.fetchone(
        "SELECT intake_submission_id FROM engagements WHERE id=?", (engagement_id,)
    )
    sub = await db.fetchone(
        "SELECT raw_json FROM intake_submissions WHERE id=?",
        (eng["intake_submission_id"],),
    ) if eng and eng["intake_submission_id"] else None

    import json as _json
    raw = _json.loads(sub["raw_json"]) if sub and sub["raw_json"] else {}
    project_type = raw.get("project_type", raw.get("projectType", "new"))
    is_new = project_type in ("new", "신규", "신규 구축", "")
    is_upgrade = project_type in ("upgrade", "고도화", "리뉴얼", "enhancement")
    scopes = set(raw.get("scope", raw.get("project_types", raw.get("projectTypes", []))))

    def _applicable(when):
        if when == "always": return True
        if when == "new": return is_new
        if when == "upgrade": return is_upgrade
        if isinstance(when, list): return bool(set(when) & scopes)
        return True

    # 의존 관계 맵
    from engine.intake.processor import INTRA_PHASE_DEPS
    dep_map = {}
    for f, t in INTRA_PHASE_DEPS:
        dep_map.setdefault(t, []).append(f)

    result = []
    for tmpl in NODE_TEMPLATES:
        phase = tmpl["phase"]
        for n in tmpl["nodes"]:
            applicable = _applicable(n.get("when", "always"))
            deps = dep_map.get(n["name"], [])
            result.append({
                "phase": phase,
                "name": n["name"],
                "model": n["model"],
                "applicable": applicable,
                "when": str(n.get("when", "always")),
                "depends_on": deps,
                "essential": n["name"] in (
                    "PRD (제품 요구사항 정의서)", "기능 백로그 (Product Backlog)",
                    "시스템 아키텍처 설계서 (HLD)", "화면 설계서 (와이어프레임+스토리보드)",
                    "API 설계서", "프론트엔드 컴포넌트 구현", "백엔드 API 구현",
                    "테스트 시나리오", "사용자 매뉴얼",
                ),
            })

    return {"deliverables": result, "total": len(result), "applicable": sum(1 for r in result if r["applicable"])}


@app.post("/api/v1/engagements/{engagement_id}/dag/start")
async def start_engagement_dag(
    engagement_id: str,
    body: dict = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """DAG를 RUNNING 상태로 전환하고 첫 번째 READY 노드를 활성화.
    body.exclude_names: 제외할 산출물 이름 리스트 (옵션).
    """
    exclude_names = set((body or {}).get("exclude_names", []))
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    # DAG 조회
    dags = await db.fetchall(
        """SELECT d.id, d.status FROM dags d
           JOIN projects p ON p.id = d.project_id
           WHERE p.engagement_id = ?""",
        (engagement_id,),
    )

    # DAG 없으면 intake_submission 기반으로 재생성
    if not dags:
        eng = await db.fetchone(
            "SELECT intake_submission_id FROM engagements WHERE id=?",
            (engagement_id,),
        )
        sub_id = eng["intake_submission_id"] if eng else None
        if not sub_id:
            raise HTTPException(status_code=404, detail="DAG 없음 — intake_submission_id 미연결")
        sub = await db.fetchone(
            "SELECT id, raw_json FROM intake_submissions WHERE id=?", (sub_id,),
        )
        if not sub:
            raise HTTPException(status_code=404, detail="DAG 없음 — intake_submission 삭제됨")

        import json as _json
        from engine.intake.processor import IntakeProcessor
        processor = IntakeProcessor(db, created_by=current_user["user_id"])
        raw = _json.loads(sub["raw_json"]) if sub["raw_json"] else {}

        # 프로젝트 조회
        projects = await db.fetchall(
            "SELECT id, component_type FROM projects WHERE engagement_id=?",
            (engagement_id,),
        )
        if not projects:
            raise HTTPException(status_code=404, detail="프로젝트 없음 — 먼저 프로젝트를 추가하세요")

        for proj in projects:
            comp = {"type": proj["component_type"] or "web_app"}
            await processor._create_dag_and_nodes(proj["id"], engagement_id, comp, raw, exclude_names=exclude_names)
            logger.info("dag_regenerated project_id=%s engagement_id=%s", proj["id"], engagement_id)

        # 재조회
        dags = await db.fetchall(
            """SELECT d.id, d.status FROM dags d
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ?""",
            (engagement_id,),
        )
        if not dags:
            raise HTTPException(status_code=500, detail="DAG 재생성 실패")

    started = []
    now = _now()
    for dag in dags:
        if dag["status"] in ("PENDING", "VALID", "PAUSED", "INITIALIZING"):
            # DAG → RUNNING
            await db.execute(
                "UPDATE dags SET status='RUNNING', current_phase='DEFINE', updated_at=? WHERE id=?",
                (now, dag["id"]),
            )

        if dag["status"] in ("PENDING", "VALID", "PAUSED", "INITIALIZING", "RUNNING"):
            # PLANNING TASK 중 선행 완료된 NOT_STARTED → READY
            dep_nodes = await db.fetchall(
                """SELECT n.id FROM nodes n
                   WHERE n.dag_id = ? AND n.state = 'NOT_STARTED'
                   AND n.phase = 'DEFINE' AND n.node_type = 'TASK'
                   AND NOT EXISTS (
                       SELECT 1 FROM edges e
                       JOIN nodes dep ON dep.id = e.from_node_id
                       WHERE e.to_node_id = n.id AND e.is_active = 1
                       AND dep.node_type != 'QA'
                   )""",
                (dag["id"],),
            )
            for n in dep_nodes:
                await db.execute(
                    "UPDATE nodes SET state='READY', updated_at=? WHERE id=? AND state='NOT_STARTED'",
                    (now, n["id"]),
                )
            # 이미 READY인 노드 수도 카운트
            ready_count = await db.fetchone(
                "SELECT COUNT(*) c FROM nodes WHERE dag_id=? AND state='READY'",
                (dag["id"],),
            )
            started.append({"dag_id": dag["id"], "ready_nodes": ready_count["c"]})

    if started:
        # Engagement → ACTIVE
        await db.execute(
            "UPDATE engagements SET status='ACTIVE', updated_at=? WHERE id=? AND status IN ('INTAKE','PAUSED')",
            (now, engagement_id),
        )
        # Projects → ACTIVE
        await db.execute(
            "UPDATE projects SET status='ACTIVE', updated_at=? WHERE engagement_id=? AND status IN ('INTAKE','PAUSED')",
            (now, engagement_id),
        )
        # DAGAdvancer에 실행 요청
        if _dag_advancer:
            for s in started:
                await _dag_advancer.enqueue(s["dag_id"])

    return {"started": started, "count": len(started)}


@app.post("/api/v1/engagements/{engagement_id}/dag/pause")
async def pause_engagement_dag(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """RUNNING DAG를 PAUSED 상태로 전환."""
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    now = _now()
    await db.execute(
        """UPDATE dags SET status='PAUSED', updated_at=?
           WHERE project_id IN (SELECT id FROM projects WHERE engagement_id=?)
           AND status='RUNNING'""",
        (now, engagement_id),
    )
    # Engagement/Project → PAUSED
    await db.execute(
        "UPDATE engagements SET status='PAUSED', updated_at=? WHERE id=? AND status='ACTIVE'",
        (now, engagement_id),
    )
    await db.execute(
        "UPDATE projects SET status='PAUSED', updated_at=? WHERE engagement_id=? AND status='ACTIVE'",
        (now, engagement_id),
    )
    return {"paused": True}


# ---------------------------------------------------------------------------
# DAG Repair & Resume
# ---------------------------------------------------------------------------

@app.post("/api/v1/engagements/{engagement_id}/clone")
async def clone_engagement_endpoint(
    engagement_id: str,
    body: dict = {},
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """
    손상된 인게이지먼트를 새 인게이지먼트로 클론.
    COMPLETED/SKIPPED 노드+artifacts 이식, 나머지는 NOT_STARTED 초기화.
    """
    from engine.lifecycle.project_cloner import clone_engagement

    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)

    new_name = body.get("name") if body else None
    result = await clone_engagement(db, engagement_id, new_name=new_name)

    # 새 DAG를 DAGAdvancer에 등록
    await _dag_advancer.enqueue(result["new_dag_id"])

    return result


@app.post("/api/v1/engagements/{engagement_id}/dag/repair-and-resume")
async def repair_and_resume_dag(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """
    손상된 DAG 복구 후 재개.
    1. 끊긴 qa_pair / task_pair 참조를 이름 매칭으로 복원.
    2. API 한도로 SUSPENDED된 노드 → BLOCKED.
    3. DAG PAUSED → RUNNING.
    """
    from engine.lifecycle.dag_repair import repair_pair_references, resume_api_suspended

    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)

    dag = await db.fetchone(
        """SELECT d.id FROM dags d
           JOIN projects p ON p.id = d.project_id
           WHERE p.engagement_id=?""",
        (engagement_id,),
    )
    if not dag:
        raise HTTPException(status_code=404, detail="DAG not found")

    dag_id = dag["id"]
    repair_result = await repair_pair_references(db, dag_id)
    resume_result = await resume_api_suspended(db, dag_id)

    # DAGAdvancer에 재처리 요청
    await _dag_advancer.enqueue(dag_id)

    return {
        "dag_id": dag_id,
        "repair": repair_result,
        "resume": resume_result,
    }


# ---------------------------------------------------------------------------
# 인게이지먼트 DAG Gate API
# ---------------------------------------------------------------------------

@app.get("/api/v1/engagements/{engagement_id}/dag/gates")
async def list_engagement_gates(
    engagement_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        """SELECT ee.id, ee.from_project_id, ee.from_phase,
                  ee.to_project_id, ee.to_phase, ee.gate_trigger_type, ee.is_active
           FROM engagement_edges ee
           JOIN engagement_dags ed ON ed.id = ee.engagement_dag_id
           WHERE ed.engagement_id=?""",
        (engagement_id,),
    )
    return {"gates": [dict(r) for r in rows]}


@app.post("/api/v1/engagements/{engagement_id}/dag/gates/{edge_id}/approve")
async def approve_engagement_gate(
    engagement_id: str,
    edge_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    row = await db.fetchone(
        "SELECT ee.to_project_id, ee.to_phase FROM engagement_edges ee JOIN engagement_dags ed ON ed.id=ee.engagement_dag_id WHERE ee.id=? AND ed.engagement_id=?",
        (edge_id, engagement_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Gate 없음")
    # 대상 프로젝트의 AWAITING_APPROVAL GATE 노드 승인
    await db.execute(
        """UPDATE nodes SET state='COMPLETED', completed_at=?, updated_at=?, version=version+1
           WHERE project_id=? AND phase=? AND node_type='GATE' AND state='AWAITING_APPROVAL'""",
        (_now(), _now(), row["to_project_id"], row["to_phase"]),
    )
    return {"approved": True}


@app.post("/api/v1/engagements/{engagement_id}/dag/gates/{edge_id}/reject")
async def reject_engagement_gate(
    engagement_id: str,
    edge_id: str,
    body: dict = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    row = await db.fetchone(
        "SELECT ee.to_project_id, ee.to_phase FROM engagement_edges ee JOIN engagement_dags ed ON ed.id=ee.engagement_dag_id WHERE ee.id=? AND ed.engagement_id=?",
        (edge_id, engagement_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Gate 없음")
    await db.execute(
        """UPDATE nodes SET state='INVALID', updated_at=?, version=version+1
           WHERE project_id=? AND phase=? AND node_type='GATE' AND state='AWAITING_APPROVAL'""",
        (_now(), row["to_project_id"], row["to_phase"]),
    )
    return {"rejected": True}


# ---------------------------------------------------------------------------
# 노드 상세 / 정지 / 재개
# ---------------------------------------------------------------------------

@app.get("/api/v1/projects/{project_id}/nodes/{node_id}")
async def get_node(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    row = await db.fetchone(
        "SELECT * FROM nodes WHERE id=? AND project_id=?", (node_id, project_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="노드 없음")
    return dict(row)


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/suspend")
async def suspend_node(
    project_id: str,
    node_id: str,
    body: dict = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.APPROVE_GATE)
    node = await db.fetchone(
        "SELECT state, version FROM nodes WHERE id=? AND project_id=?", (node_id, project_id)
    )
    if not node:
        raise HTTPException(status_code=404, detail="노드 없음")
    reason = (body or {}).get("reason", "BUDGET_EXCEEDED")
    affected = await db.execute(
        """UPDATE nodes SET state='SUSPENDED', suspension_reason=?,
           updated_at=?, version=version+1
           WHERE id=? AND version=? AND state='IN_PROGRESS'""",
        (reason, _now(), node_id, node["version"]),
    )
    if affected == 0:
        raise HTTPException(status_code=409, detail="전환 불가 상태")
    return {"status": "suspended"}


@app.post("/api/v1/projects/{project_id}/nodes/{node_id}/resume")
async def resume_node(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.RETRY_NODE)
    node = await db.fetchone(
        "SELECT state, version FROM nodes WHERE id=? AND project_id=?", (node_id, project_id)
    )
    if not node:
        raise HTTPException(status_code=404, detail="노드 없음")
    if node["state"] != "SUSPENDED":
        raise HTTPException(status_code=409, detail=f"현재 상태: {node['state']}")
    affected = await db.execute(
        """UPDATE nodes SET state='READY', suspension_reason=NULL,
           updated_at=?, version=version+1
           WHERE id=? AND version=?""",
        (_now(), node_id, node["version"]),
    )
    if affected == 0:
        raise HTTPException(status_code=409, detail="동시 수정 충돌")
    return {"status": "resumed"}


# ---------------------------------------------------------------------------
# 산출물 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/projects/{project_id}/artifacts")
async def list_artifacts(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT id, node_id, artifact_type, file_type, current_version, is_invalidated, created_at FROM artifacts WHERE project_id=? ORDER BY created_at DESC",
        (project_id,),
    )
    return {"artifacts": [dict(r) for r in rows]}


@app.get("/api/v1/projects/{project_id}/artifacts/{artifact_id}")
async def get_artifact(
    project_id: str,
    artifact_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    row = await db.fetchone(
        "SELECT * FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="산출물 없음")
    return dict(row)


@app.get("/api/v1/projects/{project_id}/artifacts/{artifact_id}/versions")
async def list_artifact_versions(
    project_id: str,
    artifact_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    # artifact 소유권 확인
    art = await db.fetchone(
        "SELECT id FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)
    )
    if not art:
        raise HTTPException(status_code=404, detail="산출물 없음")
    rows = await db.fetchall(
        "SELECT id, version_num, storage_path, content_hash, size_bytes, is_qa_approved, created_at FROM artifact_versions WHERE artifact_id=? ORDER BY version_num DESC",
        (artifact_id,),
    )
    return {"versions": [dict(r) for r in rows]}


@app.get("/api/v1/projects/{project_id}/nodes/{node_id}/artifact")
async def get_node_artifact(
    project_id: str,
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """노드의 최신 산출물 내용 반환."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    art = await db.fetchone(
        "SELECT id, artifact_type FROM artifacts WHERE node_id=? AND project_id=?",
        (node_id, project_id),
    )
    if not art:
        raise HTTPException(status_code=404, detail="산출물 없음")
    ver = await db.fetchone(
        "SELECT storage_path AS content, version_num, created_at FROM artifact_versions "
        "WHERE artifact_id=? ORDER BY version_num DESC LIMIT 1",
        (art["id"],),
    )
    if not ver:
        raise HTTPException(status_code=404, detail="산출물 버전 없음")
    return {"node_id": node_id, "content": ver["content"], "version": ver["version_num"], "created_at": ver["created_at"], "artifact_type": art["artifact_type"]}


@app.put("/api/v1/projects/{project_id}/nodes/{node_id}/artifact")
async def update_node_artifact(
    project_id: str,
    node_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """산출물 수동 수정 — 새 버전 생성 + 하위 노드 cascade invalidation."""
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    import hashlib as _hl

    new_content = body.get("content", "")
    if not new_content:
        raise HTTPException(status_code=400, detail="content 필수")

    art = await db.fetchone(
        "SELECT id, current_version FROM artifacts WHERE node_id=? AND project_id=?",
        (node_id, project_id),
    )
    if not art:
        raise HTTPException(status_code=404, detail="산출물 없음")

    now = _now()
    new_ver = (art["current_version"] or 0) + 1
    content_hash = _hl.sha256(new_content.encode("utf-8")).hexdigest()

    # artifact 버전 업
    await db.execute(
        "UPDATE artifacts SET current_version=?, updated_at=? WHERE id=?",
        (new_ver, now, art["id"]),
    )
    await db.execute(
        "INSERT INTO artifact_versions (id, artifact_id, version_num, "
        "storage_path, content_hash, size_bytes, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), art["id"], new_ver, new_content,
         content_hash, len(new_content), current_user["user_id"], now),
    )

    # diff 계산 (이전 버전 vs 새 버전)
    prev_ver = await db.fetchone(
        "SELECT storage_path AS content FROM artifact_versions "
        "WHERE artifact_id=? AND version_num=? LIMIT 1",
        (art["id"], new_ver - 1),
    )
    import difflib, json as _json
    old_text = prev_ver["content"] if prev_ver else ""
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"v{new_ver - 1}", tofile=f"v{new_ver}", lineterm="",
    ))
    diff_text = "\n".join(diff_lines[:500])
    is_empty = 1 if not diff_lines else 0

    # AI 변경 영향도 판단 (sonnet — 빠르고 저렴)
    impact_analysis = {"type": "context_change", "strategy": "full", "reason": "", "skip_cascade": False}
    if not is_empty and diff_text:
        try:
            from engine.ai.model_adapter import CLIProxyAdapter
            import shutil
            cli = shutil.which("claude")
            if cli:
                analyzer = CLIProxyAdapter(cli)
                judge_prompt = f"""아래는 프로젝트 산출물의 변경 diff입니다. 이 변경의 성격을 판단하세요.

```diff
{diff_text[:2000]}
```

아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{"type": "타입", "reason": "판단 이유 한 줄", "strategy": "full 또는 diff", "skip_cascade": true 또는 false}}

타입 판단 기준 (6단계):

[변경]
- context_change: 비즈니스 로직, 구조, 프로세스 흐름 변경 → strategy: "full", skip_cascade: false
- minor_fix: 오타, 날짜, 수치 보정, 포맷 변경 → strategy: "diff", skip_cascade: true

[추가]
- add_major: 핵심 기능 추가, 사용자 역할 추가, 데이터 모델에 영향 있는 추가 → strategy: "full", skip_cascade: false
- add_minor: 부가 설명 보충, 예시 추가, 참고사항 추가 → strategy: "diff", skip_cascade: true

[삭제]
- delete_major: 핵심 기능 삭제, 모듈 제거, 역할 제거 등 하위 의존 있는 삭제 → strategy: "full", skip_cascade: false
- delete_minor: 불필요한 예시 제거, 중복 내용 정리, 주석 삭제 → strategy: "diff", skip_cascade: true

핵심: "이 변경/추가/삭제로 하위 산출물(요구사항→설계→개발)의 내용이 바뀌어야 하는가?" 를 기준으로 판단."""

                resp = await analyzer.call(
                    model="claude-haiku-4-5-20251001",
                    prompt=judge_prompt, max_tokens=200, temperature=0,
                )
                # JSON 파싱
                import re
                json_match = re.search(r'\{[^}]+\}', resp.content)
                if json_match:
                    impact_analysis = _json.loads(json_match.group())
        except Exception as judge_err:
            logger.warning("impact_analysis_failed error=%s", str(judge_err))
            # 판단 실패 시 안전하게 full cascade

    # cascade invalidation — AI 판단에 따라 분기
    invalidated = 0
    downstream_ids = []
    strategy = impact_analysis.get("strategy", "full")
    skip_cascade = impact_analysis.get("skip_cascade", False)

    if not skip_cascade:
        downstream = await db.fetchall(
            """SELECT n.id, n.state FROM edges e
               JOIN nodes n ON n.id = e.to_node_id
               WHERE e.from_node_id = ? AND e.is_active = 1
                 AND n.state IN ('COMPLETED', 'IN_PROGRESS', 'READY')""",
            (node_id,),
        )
        for d in downstream:
            await db.execute(
                "UPDATE nodes SET state='INVALID', updated_at=?, version=version+1 WHERE id=?",
                (now, d["id"]),
            )
            downstream_ids.append(d["id"])
            invalidated += 1

    # delta 생성 — strategy에 따라 diff 또는 전체 문서 저장
    if not is_empty and downstream_ids:
        delta_content = diff_text if strategy == "diff" else f"[전체 문서 — 맥락 변경]\n{new_content[:8000]}"
        delta_strategy = "unified_diff" if strategy == "diff" else "full_document"
        await db.execute(
            """INSERT INTO deltas (id, artifact_id, from_version_num, to_version_num,
               diff_strategy, delta_content, delta_size_bytes, is_empty,
               impacted_node_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), art["id"], new_ver - 1, new_ver,
             delta_strategy, delta_content, len(delta_content), is_empty,
             _json.dumps(downstream_ids), now),
        )

    # DAG 재큐 (INVALID 노드 재실행 트리거)
    if invalidated and _dag_advancer:
        dag_row = await db.fetchone(
            "SELECT dag_id FROM nodes WHERE id=?", (node_id,)
        )
        if dag_row:
            await _dag_advancer.enqueue(dag_row["dag_id"])

    # FAILED 노드를 수동 수정한 경우 → COMPLETED 전환
    node_state = await db.fetchone("SELECT state, qa_pair_node_id FROM nodes WHERE id=?", (node_id,))
    if node_state and node_state["state"] == "FAILED":
        now2 = _now()
        await db.execute(
            "UPDATE nodes SET state='COMPLETED', completed_at=?, retry_count=0, "
            "failure_reasons='[]', updated_at=?, version=version+1 WHERE id=?",
            (now2, now2, node_id),
        )
        # QA 쌍도 COMPLETED
        if node_state["qa_pair_node_id"]:
            await db.execute(
                "UPDATE nodes SET state='COMPLETED', completed_at=?, retry_count=0, "
                "updated_at=?, version=version+1 WHERE id=?",
                (now2, now2, node_state["qa_pair_node_id"]),
            )
        # DAG 재큐
        if _dag_advancer:
            dag_row2 = await db.fetchone("SELECT dag_id FROM nodes WHERE id=?", (node_id,))
            if dag_row2:
                await _dag_advancer.enqueue(dag_row2["dag_id"])

    logger.info("artifact_edited node_id=%s version=%d invalidated=%d strategy=%s user=%s",
                node_id, new_ver, invalidated, strategy, current_user["user_id"])
    return {
        "version": new_ver,
        "invalidated": invalidated,
        "impact": impact_analysis.get("type", "unknown"),
        "reason": impact_analysis.get("reason", ""),
        "strategy": strategy,
    }


@app.get("/api/v1/projects/{project_id}/artifacts/{artifact_id}/diff")
async def artifact_diff(
    project_id: str,
    artifact_id: str,
    from_version: int,
    to_version: int,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    art = await db.fetchone(
        "SELECT id FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)
    )
    if not art:
        raise HTTPException(status_code=404, detail="산출물 없음")
    delta = await db.fetchone(
        "SELECT * FROM deltas WHERE artifact_id=? AND from_version_num=? AND to_version_num=?",
        (artifact_id, from_version, to_version),
    )
    if not delta:
        raise HTTPException(status_code=404, detail="Delta 없음")
    return dict(delta)


# ---------------------------------------------------------------------------
# 크리덴셜 관리 API
# ---------------------------------------------------------------------------

def _get_encrypt_key() -> str:
    """PLATFORM_ENCRYPT_KEY 환경변수에서 암호화 키 로드. 미설정 시 예외."""
    from engine.security.crypto import AES256GCM
    return AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")


@app.get("/api/v1/credentials")
async def list_credentials(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """크리덴셜 목록 (key_encrypted/oauth_config_encrypted 제외, key_preview만)."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    rows = await db.fetchall(
        "SELECT id, name, provider, auth_mode, key_preview, is_active, is_default, "
        "last_used_at, token_expires_at, usage_count, created_at "
        "FROM provider_credentials ORDER BY created_at DESC"
    )
    return {"credentials": [dict(r) for r in rows]}


@app.post("/api/v1/credentials", status_code=201)
async def create_credential(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """새 크리덴셜 등록. body: {name, provider, auth_mode, api_key?, oauth_config?, is_default?}"""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    from engine.security.crypto import AES256GCM

    provider = body.get("provider", "anthropic")
    if provider not in ("anthropic", "openai", "google"):
        raise HTTPException(status_code=400, detail="provider는 anthropic, openai, google 중 하나여야 합니다")
    auth_mode = body.get("auth_mode", "api_key")
    if auth_mode not in ("api_key", "oauth"):
        raise HTTPException(status_code=400, detail="auth_mode는 api_key 또는 oauth여야 합니다")

    try:
        enc_key = _get_encrypt_key()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    cid = str(uuid.uuid4())
    now = _now()
    key_encrypted = None
    key_hash = None
    key_preview = None
    oauth_config_encrypted = None

    if auth_mode == "api_key":
        raw_key = body.get("api_key", "")
        if not raw_key:
            raise HTTPException(status_code=400, detail="api_key 모드에서는 api_key 필드가 필수입니다")
        key_preview = raw_key[:8] + "..." + raw_key[-4:] if len(raw_key) > 12 else "***"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_encrypted = AES256GCM.encrypt(raw_key, enc_key)
    elif auth_mode == "oauth":
        oauth_config = body.get("oauth_config")
        if not oauth_config:
            raise HTTPException(status_code=400, detail="oauth 모드에서는 oauth_config 필드가 필수입니다")
        if isinstance(oauth_config, dict):
            oauth_config = json.dumps(oauth_config)
        oauth_config_encrypted = AES256GCM.encrypt(oauth_config, enc_key)

    is_default = 1 if body.get("is_default") else 0
    if is_default:
        await db.execute(
            "UPDATE provider_credentials SET is_default=0, updated_at=? WHERE provider=?",
            (now, provider),
        )

    await db.execute(
        """INSERT INTO provider_credentials
           (id, name, provider, auth_mode, key_hash, key_encrypted, key_preview,
            oauth_config_encrypted, is_active, is_default, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)""",
        (cid, body.get("name", provider), provider, auth_mode,
         key_hash, key_encrypted, key_preview, oauth_config_encrypted,
         is_default, current_user["user_id"], now, now),
    )
    return {"id": cid, "name": body.get("name", provider), "provider": provider, "key_preview": key_preview}


@app.put("/api/v1/credentials/{credential_id}")
async def update_credential(
    credential_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """크리덴셜 수정. body: {name?, api_key?, oauth_config?, is_active?, is_default?}"""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    from engine.security.crypto import AES256GCM

    row = await db.fetchone(
        "SELECT id, provider, auth_mode FROM provider_credentials WHERE id=?",
        (credential_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="크리덴셜 없음")

    sets = []
    vals = []
    now = _now()

    if "name" in body:
        sets.append("name=?")
        vals.append(body["name"])

    if "is_active" in body:
        sets.append("is_active=?")
        vals.append(1 if body["is_active"] else 0)

    if "is_default" in body and body["is_default"]:
        await db.execute(
            "UPDATE provider_credentials SET is_default=0, updated_at=? WHERE provider=?",
            (now, row["provider"]),
        )
        sets.append("is_default=?")
        vals.append(1)
    elif "is_default" in body:
        sets.append("is_default=?")
        vals.append(0)

    if body.get("api_key"):
        try:
            enc_key = _get_encrypt_key()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        raw_key = body["api_key"]
        key_preview = raw_key[:8] + "..." + raw_key[-4:] if len(raw_key) > 12 else "***"
        sets.append("key_encrypted=?")
        vals.append(AES256GCM.encrypt(raw_key, enc_key))
        sets.append("key_hash=?")
        vals.append(hashlib.sha256(raw_key.encode()).hexdigest())
        sets.append("key_preview=?")
        vals.append(key_preview)

    if body.get("oauth_config"):
        try:
            enc_key = _get_encrypt_key()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        oauth_config = body["oauth_config"]
        if isinstance(oauth_config, dict):
            oauth_config = json.dumps(oauth_config)
        sets.append("oauth_config_encrypted=?")
        vals.append(AES256GCM.encrypt(oauth_config, enc_key))

    if not sets:
        raise HTTPException(status_code=400, detail="수정할 필드 없음")

    sets.append("updated_at=?")
    vals.append(now)
    vals.append(credential_id)
    await db.execute(
        f"UPDATE provider_credentials SET {', '.join(sets)} WHERE id=?",
        tuple(vals),
    )
    return {"updated": True}


@app.delete("/api/v1/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """크리덴셜 삭제."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    await db.execute(
        "DELETE FROM provider_credentials WHERE id=?",
        (credential_id,),
    )


@app.post("/api/v1/credentials/{credential_id}/test")
async def test_credential(
    credential_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """크리덴셜 연결 테스트 — 간단한 API 호출로 유효성 확인."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    row = await db.fetchone(
        "SELECT id, provider, auth_mode, key_encrypted, oauth_config_encrypted, token_expires_at "
        "FROM provider_credentials WHERE id=?",
        (credential_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="크리덴셜 없음")

    try:
        enc_key = _get_encrypt_key()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    from engine.security.crypto import AES256GCM
    try:
        from engine.ai.model_adapter import ModelAdapter

        if row["auth_mode"] == "api_key" and row["key_encrypted"]:
            decrypted_key = AES256GCM.decrypt(row["key_encrypted"], enc_key)
            from engine.ai.model_adapter import AnthropicPlaintextKeyProvider, AnthropicAPIKeyProvider
            if row["provider"] == "anthropic":
                creds = AnthropicPlaintextKeyProvider(decrypted_key)
            else:
                creds = AnthropicPlaintextKeyProvider(decrypted_key)
            adapter = ModelAdapter(creds)
        elif row["auth_mode"] == "oauth" and row["oauth_config_encrypted"]:
            from engine.ai.model_adapter import OAuthProvider
            creds = OAuthProvider(
                oauth_config_encrypted=row["oauth_config_encrypted"],
                token_expires_at=row["token_expires_at"],
            )
            adapter = ModelAdapter(creds)
        else:
            return {"success": False, "error": "유효한 인증 정보가 없습니다"}

        result = await adapter.generate(
            messages=[{"role": "user", "content": "test"}],
            model=ModelID.SONNET if row["provider"] == "anthropic" else None,
            max_tokens=10,
        )
        # 사용 횟수 증가 + last_used_at 갱신
        now = _now()
        await db.execute(
            "UPDATE provider_credentials SET usage_count=usage_count+1, last_used_at=?, updated_at=? WHERE id=?",
            (now, now, credential_id),
        )
        return {"success": True, "model": row["provider"]}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/v1/credentials/{credential_id}/refresh")
async def refresh_credential(
    credential_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """OAuth 토큰 갱신."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    row = await db.fetchone(
        "SELECT provider, auth_mode, oauth_config_encrypted FROM provider_credentials WHERE id=? AND is_active=1",
        (credential_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="크리덴셜 없음")
    if row["auth_mode"] != "oauth":
        raise HTTPException(status_code=400, detail="OAuth 모드만 갱신 가능")

    try:
        enc_key = _get_encrypt_key()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    from engine.security.crypto import AES256GCM

    if not row["oauth_config_encrypted"]:
        raise HTTPException(status_code=503, detail="OAuth 설정 없음")
    config = json.loads(AES256GCM.decrypt(row["oauth_config_encrypted"], enc_key))
    provider = row["provider"]

    from engine.auth.oauth import refresh_anthropic_token, refresh_openai_token
    if provider == "anthropic":
        token_data = await refresh_anthropic_token(
            config["refresh_token"], config["client_id"], config["client_secret"]
        )
    elif provider == "openai":
        token_data = await refresh_openai_token(
            config["refresh_token"], config["client_id"], config["client_secret"]
        )
    else:
        raise HTTPException(status_code=400, detail=f"미지원 provider: {provider}")
    await db.execute(
        "UPDATE provider_credentials SET token_expires_at=?, usage_count=usage_count+1, last_used_at=?, updated_at=? WHERE id=?",
        (token_data.get("expires_at", ""), _now(), _now(), credential_id),
    )
    return {"refreshed": True, "expires_at": token_data.get("expires_at")}


# ---------------------------------------------------------------------------
# CLI 계정 관리 API (Pro/Max 구독 다계정)
# ---------------------------------------------------------------------------

@app.post("/api/v1/system/reload-adapter")
async def reload_adapter(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """CLI 어댑터를 최신 계정으로 재로드 (런타임 계정 전환)."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    global _dag_advancer
    import shutil
    from engine.ai.model_adapter import CLIProxyAdapter
    from engine.skills.executor import create_skill_executor
    from engine.ai.context_assembler import ContextAssembler

    cli_path = shutil.which("claude")
    if not cli_path:
        raise HTTPException(status_code=503, detail="claude CLI 없음")

    # 최신 우선순위 계정 조회
    first = await db.fetchone(
        "SELECT name, tier, config_dir FROM cli_accounts WHERE is_active=1 ORDER BY priority ASC LIMIT 1"
    )
    if first and first["config_dir"]:
        adapter = CLIProxyAdapter(cli_path, config_dir=first["config_dir"])
    else:
        adapter = CLIProxyAdapter(cli_path)

    account_name = first["name"] if first else "기본"
    tier = first["tier"] if first else "?"

    # executor + advancer 재생성
    assembler = ContextAssembler()
    executor_fn = await create_skill_executor(db, assembler, adapter)

    if _dag_advancer:
        await _dag_advancer.stop()

    from engine.core.dag_advancer import DAGAdvancer
    import asyncio as _asyncio
    _dag_advancer = DAGAdvancer(db, executor_fn)
    _asyncio.create_task(_dag_advancer.run())

    logger.info("adapter_reloaded account=%s tier=%s", account_name, tier)
    return {"account": account_name, "tier": tier}


@app.get("/api/v1/cli-accounts")
async def list_cli_accounts(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """등록된 Claude Code CLI 계정 목록."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    rows = await db.fetchall(
        "SELECT id, name, tier, config_dir, is_active, priority, created_at, last_used_at "
        "FROM cli_accounts ORDER BY priority ASC, created_at ASC"
    )
    return {"cli_accounts": [dict(r) for r in rows]}


@app.post("/api/v1/cli-accounts", status_code=201)
async def create_cli_account(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """CLI 계정 등록. body: {name, tier, config_dir?, priority?}
    config_dir: CLAUDE_CONFIG_DIR 경로. 없으면 기본 ~/.claude (현재 로그인 계정).
    """
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    import uuid as _uuid

    name = body.get("name", "").strip()
    tier = body.get("tier", "pro")
    if not name:
        raise HTTPException(status_code=400, detail="name은 필수입니다")
    if tier not in ("pro", "max"):
        raise HTTPException(status_code=400, detail="tier는 pro 또는 max여야 합니다")

    config_dir = body.get("config_dir") or None
    priority = int(body.get("priority", 0))
    row_id = f"cli_{_uuid.uuid4().hex[:12]}"

    await db.execute(
        "INSERT INTO cli_accounts (id, name, tier, config_dir, is_active, priority) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (row_id, name, tier, config_dir, priority),
    )
    return {"id": row_id, "name": name, "tier": tier, "config_dir": config_dir, "priority": priority}


@app.patch("/api/v1/cli-accounts/{account_id}", status_code=200)
async def update_cli_account(
    account_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """CLI 계정 부분 수정. body: {is_active?, priority?, name?, tier?}"""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    sets, vals = [], []
    for col in ("is_active", "priority", "name", "tier"):
        if col in body:
            sets.append(f"{col}=?")
            vals.append(body[col])
    if not sets:
        raise HTTPException(status_code=400, detail="변경할 필드가 없습니다")
    vals.append(account_id)
    await db.execute(f"UPDATE cli_accounts SET {', '.join(sets)} WHERE id=?", tuple(vals))
    return {"ok": True}


@app.delete("/api/v1/cli-accounts/{account_id}", status_code=204)
async def delete_cli_account(
    account_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """CLI 계정 삭제."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    await db.execute("DELETE FROM cli_accounts WHERE id=?", (account_id,))


@app.post("/api/v1/cli-accounts/detect")
async def detect_cli_account(
    current_user: dict = Depends(get_current_user),
):
    """현재 `claude auth status`로 로그인된 계정 정보 반환."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    import asyncio
    import json
    import shutil

    cli = shutil.which("claude")
    if not cli:
        raise HTTPException(status_code=404, detail="claude CLI를 찾을 수 없습니다")

    proc = await asyncio.create_subprocess_exec(
        cli, "auth", "status", "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    try:
        info = json.loads(stdout.decode())
    except Exception:
        info = {"raw": stdout.decode()}
    return info


# ---------------------------------------------------------------------------
# 리소스 관리 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/resources/global")
async def list_global_resources(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    rows = await db.fetchall(
        "SELECT key, description, is_secret, created_at FROM project_env_vars WHERE scope='GLOBAL' ORDER BY key"
    )
    return {"resources": [dict(r) for r in rows]}


@app.post("/api/v1/resources/global", status_code=201)
async def set_global_resource(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    from engine.core.resource_resolver import ResourceResolver
    import os
    resolver = ResourceResolver(db)
    await resolver.set(
        scope="GLOBAL", scope_id="GLOBAL",
        key=body["key"], value=body["value"],
        created_by=current_user["user_id"],
        is_secret=body.get("is_secret", True),
        description=body.get("description", ""),
    )
    return {"set": True}


@app.delete("/api/v1/resources/global/{key}", status_code=204)
async def delete_global_resource(
    key: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)
    await db.execute(
        "DELETE FROM project_env_vars WHERE scope='GLOBAL' AND scope_id='GLOBAL' AND key=?",
        (key,),
    )


@app.get("/api/v1/projects/{project_id}/resources/resolved")
async def get_resolved_resources(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    proj = await db.fetchone(
        "SELECT id, engagement_id FROM projects WHERE id=?", (project_id,)
    )
    if not proj:
        raise HTTPException(status_code=404, detail="프로젝트 없음")
    from engine.core.resource_resolver import ResourceResolver, NODE_REQUIRED_KEYS
    import os
    resolver = ResourceResolver(db, encryption_key=os.environ.get("ENCRYPTION_KEY"))
    all_keys = {k for keys in NODE_REQUIRED_KEYS.values() for k in keys}
    result = {}
    for key in all_keys:
        val = await resolver.resolve(key, project_id, proj["engagement_id"] or "")
        result[key] = {"found": val is not None, "scope": "resolved"}
    return {"resources": result}


@app.get("/api/v1/projects/{project_id}/resources/missing")
async def get_missing_resources(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    proj = await db.fetchone(
        "SELECT id, engagement_id, phase FROM projects WHERE id=?", (project_id,)
    )
    if not proj:
        raise HTTPException(status_code=404, detail="프로젝트 없음")
    from engine.core.resource_resolver import ResourceResolver
    import os
    resolver = ResourceResolver(db, encryption_key=os.environ.get("ENCRYPTION_KEY"))
    try:
        await resolver.validate_required_for_phase(
            project_id, proj["engagement_id"] or "", proj["phase"]
        )
        return {"missing": []}
    except Exception as exc:
        return {"missing": [str(exc)]}


# ---------------------------------------------------------------------------
# 알림 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/alerts")
async def list_alerts(
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    from engine.observability.alert_rules import AlertManager
    rows = await db.fetchall(
        """SELECT id, project_id, node_id, escalation_type, severity,
                  title, description, status, created_at
           FROM escalations WHERE status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS')
           ORDER BY severity DESC, created_at DESC LIMIT 50"""
    )
    return {"alerts": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 메트릭 엔드포인트 (Prometheus)
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics():
    """Prometheus scrape 엔드포인트."""
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi.responses import Response
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except ImportError:
        raise HTTPException(status_code=503, detail="prometheus_client 미설치")


# ---------------------------------------------------------------------------
# OAuth 연결 Flow
# ---------------------------------------------------------------------------

# OAuth 설정 (환경변수 또는 기본값)
OAUTH_CONFIGS = {
    "anthropic": {
        "authorize_url": "https://console.anthropic.com/oauth/authorize",
        "token_url": "https://api.anthropic.com/oauth/token",
        "client_id": os.environ.get("ANTHROPIC_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("ANTHROPIC_OAUTH_CLIENT_SECRET", ""),
        "scope": "api:read api:write",
    },
    "openai": {
        "authorize_url": "https://platform.openai.com/oauth/authorize",
        "token_url": "https://api.openai.com/oauth/token",
        "client_id": os.environ.get("OPENAI_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("OPENAI_OAUTH_CLIENT_SECRET", ""),
        "scope": "model.read model.write",
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "scope": "https://www.googleapis.com/auth/cloud-platform",
    },
}

@app.get("/api/v1/oauth/{provider}/authorize")
async def oauth_start(provider: str, request: Request):
    """OAuth 인증 시작 — 프론트에서 리다이렉트할 URL 반환."""
    if provider not in OAUTH_CONFIGS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 Provider: {provider}")

    config = OAUTH_CONFIGS[provider]
    if not config["client_id"]:
        raise HTTPException(status_code=400, detail=f"{provider} OAuth가 설정되지 않았습니다. 환경변수를 확인하세요.")

    state = secrets.token_urlsafe(32)
    # state를 세션이나 임시 저장 (간단히 메모리에)
    if not hasattr(app, '_oauth_states'):
        app._oauth_states = {}
    app._oauth_states[state] = {"provider": provider, "created": _now()}

    # callback URL
    base_url = str(request.base_url).rstrip('/')
    redirect_uri = f"{base_url}/api/v1/oauth/callback"

    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": config["scope"],
        "state": state,
    }
    # Google needs access_type=offline for refresh_token
    if provider == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"

    from urllib.parse import urlencode
    authorize_url = config["authorize_url"] + "?" + urlencode(params)

    return {"authorize_url": authorize_url, "state": state}


@app.get("/api/v1/oauth/callback")
async def oauth_callback(
    code: str = None,
    state: str = None,
    error: str = None,
    request: Request = None,
    db: DatabaseAdapter = Depends(get_db),
):
    """OAuth 콜백 — 코드를 토큰으로 교환 후 DB 저장."""
    if error:
        # 에러 페이지로 리다이렉트
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/credentials?error={error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="code와 state가 필요합니다")

    # state 검증
    if not hasattr(app, '_oauth_states') or state not in app._oauth_states:
        raise HTTPException(status_code=400, detail="유효하지 않은 state")

    state_data = app._oauth_states.pop(state)
    provider = state_data["provider"]
    config = OAUTH_CONFIGS[provider]

    base_url = str(request.base_url).rstrip('/')
    redirect_uri = f"{base_url}/api/v1/oauth/callback"

    # 코드 → 토큰 교환
    try:
        if provider == "anthropic":
            from engine.auth.oauth import exchange_anthropic_code
            token_data = await exchange_anthropic_code(
                code=code,
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                redirect_uri=redirect_uri,
            )
        elif provider == "google":
            # Google은 별도 처리
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(config["token_url"], json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "redirect_uri": redirect_uri,
                }) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise Exception(f"토큰 교환 실패: {text[:200]}")
                    token_data = await resp.json()
        else:
            # OpenAI 등
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(config["token_url"], json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "redirect_uri": redirect_uri,
                }) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise Exception(f"토큰 교환 실패: {text[:200]}")
                    token_data = await resp.json()
    except Exception as exc:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/credentials?error={str(exc)[:100]}")

    # DB에 크리덴셜 저장
    try:
        from engine.security.crypto import AES256GCM
        encrypt_key = AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")

        oauth_config = {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": token_data.get("refresh_token", ""),
            "token_url": config["token_url"],
            "access_token": token_data.get("access_token", ""),
        }

        encrypted = AES256GCM.encrypt(json.dumps(oauth_config), encrypt_key)

        cred_id = str(uuid.uuid4())
        now = _now()
        expires_at = token_data.get("expires_at") or ""
        if not expires_at and token_data.get("expires_in"):
            exp = datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))
            expires_at = exp.isoformat()

        # 기존 같은 provider 기본 크리덴셜 해제
        await db.execute(
            "UPDATE provider_credentials SET is_default=0 WHERE provider=?",
            (provider,)
        )

        await db.execute(
            """INSERT INTO provider_credentials
               (id, name, provider, auth_mode, oauth_config_encrypted,
                is_active, is_default, token_expires_at, created_by, created_at, updated_at)
               VALUES (?,?,?,'oauth',?,1,1,?,?,?,?)""",
            (cred_id, f"{provider.title()} OAuth", provider, encrypted,
             expires_at, "system", now, now),
        )
    except Exception as exc:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/credentials?error=저장실패:{str(exc)[:80]}")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/credentials?connected=" + provider)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Claude Code Keychain 토큰 가져오기
# ---------------------------------------------------------------------------

@app.post("/api/v1/credentials/import-claude-code")
async def import_claude_code_credentials(
    body: dict = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """macOS Keychain에서 Claude Code OAuth 토큰을 읽어 DB에 저장."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    import subprocess as _sp

    # Keychain에서 토큰 읽기
    try:
        result = _sp.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=404, detail="Claude Code 인증 정보를 찾을 수 없습니다. Claude Code에서 /login을 먼저 실행하세요.")
        raw = result.stdout.strip()
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="macOS 환경에서만 사용 가능합니다 (security 명령어 필요)")
    except _sp.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Keychain 접근 시간 초과")

    import json as _json
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Keychain 데이터 파싱 실패")

    oauth_data = data.get("claudeAiOauth")
    if not oauth_data or not oauth_data.get("accessToken"):
        raise HTTPException(status_code=404, detail="Claude Code에 로그인된 계정이 없습니다")

    access_token = oauth_data["accessToken"]
    refresh_token = oauth_data.get("refreshToken", "")
    expires_at = oauth_data.get("expiresAt", 0)
    sub_type = oauth_data.get("subscriptionType", "unknown")
    scopes = oauth_data.get("scopes", [])

    # 별명 결정 (body에서 받거나 자동)
    alias = (body or {}).get("name", "").strip()
    if not alias:
        alias = f"Claude {sub_type.title()}"

    # 만료 시각 변환
    expires_iso = ""
    if expires_at:
        from datetime import datetime as _dt, timezone as _tz
        expires_iso = _dt.fromtimestamp(expires_at / 1000, tz=_tz.utc).isoformat()

    # 토큰 미리보기
    key_preview = access_token[:12] + "..." + access_token[-4:] if len(access_token) > 16 else access_token

    # 이미 동일 토큰이 등록되어 있는지 확인 (key_hash)
    token_hash = hashlib.sha256(access_token.encode()).hexdigest()
    existing = await db.fetchone(
        "SELECT id, name FROM provider_credentials WHERE key_hash=?", (token_hash,)
    )
    if existing:
        # 토큰 갱신만 (expires_at 업데이트)
        now = _now()
        oauth_config = _json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "subscription_type": sub_type,
            "scopes": scopes,
        })
        try:
            from engine.security.crypto import AES256GCM
            encrypt_key = AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")
            encrypted = AES256GCM.encrypt(oauth_config, encrypt_key)
        except Exception:
            encrypted = ""  # 암호화 키 없으면 빈 값

        await db.execute(
            """UPDATE provider_credentials
               SET oauth_config_encrypted=?, key_preview=?, token_expires_at=?,
                   updated_at=?, key_hash=?
               WHERE id=?""",
            (encrypted, key_preview, expires_iso, now, token_hash, existing["id"]),
        )
        return {"imported": True, "updated": True, "name": existing["name"],
                "subscription": sub_type, "id": existing["id"]}

    # 신규 등록
    cred_id = str(uuid.uuid4())
    now = _now()
    oauth_config = _json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "subscription_type": sub_type,
        "scopes": scopes,
    })
    try:
        from engine.security.crypto import AES256GCM
        encrypt_key = AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")
        encrypted = AES256GCM.encrypt(oauth_config, encrypt_key)
    except Exception:
        encrypted = ""

    await db.execute(
        """INSERT INTO provider_credentials
           (id, name, provider, auth_mode, key_hash, key_preview,
            oauth_config_encrypted, is_active, is_default,
            token_expires_at, created_by, created_at, updated_at)
           VALUES (?,?,?,'oauth',?,?,?,1,0,?,?,?,?)""",
        (cred_id, alias, "anthropic", token_hash, key_preview,
         encrypted, expires_iso, current_user["user_id"], now, now),
    )
    return {"imported": True, "updated": False, "name": alias,
            "subscription": sub_type, "id": cred_id}


# ---------------------------------------------------------------------------
# 스킬 관리 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/skills")
async def list_skills(current_user: dict = Depends(get_current_user)):
    """전체 스킬(산출물 프롬프트) 목록 반환."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    from engine.skills.registry import SkillRegistry
    registry = SkillRegistry()
    return {"skills": registry.list_all()}


@app.get("/api/v1/skills/{phase}/{name}")
async def get_skill(
    phase: str, name: str,
    current_user: dict = Depends(get_current_user),
):
    """특정 스킬 상세 조회."""
    RBAC.require(current_user["role"], Permission.VIEW_PROJECT)
    from engine.skills.registry import SkillRegistry
    registry = SkillRegistry()
    spec = registry.get_spec(phase, name)
    if not spec:
        raise HTTPException(status_code=404, detail="스킬 없음")
    return spec


@app.put("/api/v1/skills/{phase}/{name}")
async def update_skill(
    phase: str, name: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """스킬 프롬프트/검증 규칙 수정."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    from engine.skills.registry import SkillRegistry
    registry = SkillRegistry()
    registry.save_spec(phase, name, body)
    return {"saved": True, "phase": phase, "name": name}


@app.post("/api/v1/skills/{phase}")
async def create_skill(
    phase: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """새 스킬 생성."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    skill_name = body.get("name", "").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail="스킬 이름 필수")
    from engine.skills.registry import SkillRegistry
    registry = SkillRegistry()
    filename = skill_name.replace(" ", "_").replace("(", "").replace(")", "").replace("·", "_")
    registry.save_spec(phase, filename, body)
    return {"created": True, "phase": phase, "name": filename}


@app.delete("/api/v1/skills/{phase}/{name}", status_code=204)
async def delete_skill(
    phase: str, name: str,
    current_user: dict = Depends(get_current_user),
):
    """스킬 삭제."""
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)
    from engine.skills.registry import SkillRegistry
    registry = SkillRegistry()
    if not registry.delete_spec(phase, name):
        raise HTTPException(status_code=404, detail="스킬 없음")


# ---------------------------------------------------------------------------
# v10 컴포넌트 조합 — revise 후처리
# ---------------------------------------------------------------------------

async def _composition_revise_hook(
    db, node_id: str, project_id: str, new_content: str
) -> None:
    """composition 노드가 수정되면 registry 업데이트 + 영향받는 페이지 재조립.

    동작:
      1. 노드의 스킬 스펙에서 composition_role 확인
      2. role이 tokens/library/recipe면 → JSON 파싱 → registry 저장
      3. diff_impact()로 영향받는 페이지 파악
      4. 영향받는 페이지만 재조립 → artifact 업데이트

    코어 불변: DAGAdvancer, StateMachine, cascade 미변경.
    """
    import json as _json_comp

    # 노드의 스킬 스펙 확인
    node_row = await db.fetchone(
        "SELECT name, phase, node_type FROM nodes WHERE id=?", (node_id,)
    )
    if not node_row:
        return

    from engine.skills.registry import SkillRegistry
    skill_registry = SkillRegistry()
    spec = skill_registry.resolve(node_row["name"], node_row["phase"], node_row["node_type"])

    if not spec or not spec.get("composition_role"):
        return

    role = spec["composition_role"]

    # assembly 노드 자체는 revise 대상이 아님 (재조립은 아래에서 트리거)
    if role == "assembly":
        return

    # registry/mapping은 중간 산출물 — 저장만
    if role == "registry":
        return

    from engine.composition.registry import (
        CompositionRegistry,
        _dict_to_tokens,
        _dict_to_component,
        _dict_to_recipe,
    )
    from engine.composition.renderer import CompositionRenderer

    comp_registry = CompositionRegistry(db)

    # JSON 추출
    from engine.skills.executor import _extract_json
    clean_json = _extract_json(new_content)
    if not clean_json:
        logger.warning("composition_revise_json_extract_failed node=%s role=%s", node_id, role)
        return

    try:
        parsed = _json_comp.loads(clean_json)
    except (ValueError, _json_comp.JSONDecodeError):
        logger.warning("composition_revise_json_invalid node=%s role=%s", node_id, role)
        return

    # registry 업데이트
    if role == "tokens":
        tokens_dict = parsed if isinstance(parsed, dict) else {}
        tokens_dict["project_id"] = project_id
        tokens = _dict_to_tokens(tokens_dict)
        tokens.project_id = project_id
        await comp_registry.save_tokens(tokens)
        changed = "tokens"

    elif role == "library":
        components = parsed if isinstance(parsed, list) else [parsed]
        for comp_dict in components:
            comp = _dict_to_component(comp_dict)
            await comp_registry.save_component(project_id, comp)
        # 변경된 컴포넌트 이름들 (모두 재조립)
        changed = "tokens"  # library 전체 변경 시 모든 페이지 영향

    elif role == "recipe":
        recipes = parsed if isinstance(parsed, list) else [parsed]
        for recipe_dict in recipes:
            recipe_dict["project_id"] = project_id
            recipe = _dict_to_recipe(recipe_dict)
            recipe.project_id = project_id
            await comp_registry.save_recipe(recipe)
        changed = "tokens"  # 레시피 변경도 전체 재조립

    else:
        return

    # 영향받는 페이지 재조립
    renderer = CompositionRenderer(comp_registry)
    affected_slugs = await renderer.diff_impact(project_id, changed)

    if not affected_slugs:
        return

    # 조립 노드의 artifact 찾기
    assembly_node = await db.fetchone(
        "SELECT n.id FROM nodes n WHERE n.project_id=? AND n.name='페이지 조립'",
        (project_id,),
    )

    if not assembly_node:
        logger.info("composition_revise_no_assembly_node project=%s", project_id)
        return

    # 영향받는 페이지만 재렌더링
    reassembled = 0
    for slug in affected_slugs:
        result = await renderer.render_page(project_id, slug)
        if result.html:
            # 기존 artifact에 새 버전으로 저장
            import hashlib as _hl_comp
            now = _now()
            art = await db.fetchone(
                "SELECT id, current_version FROM artifacts WHERE node_id=?",
                (assembly_node["id"],),
            )
            if art:
                new_ver = (art["current_version"] or 0) + 1
                await db.execute(
                    "UPDATE artifacts SET current_version=?, updated_at=? WHERE id=?",
                    (new_ver, now, art["id"]),
                )
                await db.execute(
                    "INSERT INTO artifact_versions (id, artifact_id, version_num, "
                    "storage_path, content_hash, size_bytes, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'composition-reassembly', ?)",
                    (str(uuid.uuid4()), art["id"], new_ver, result.html,
                     _hl_comp.sha256(result.html.encode()).hexdigest(),
                     len(result.html), now),
                )
                reassembled += 1

    logger.info(
        "composition_revise_reassembled project=%s role=%s pages=%d",
        project_id, role, reassembled,
    )


# ---------------------------------------------------------------------------
# v10 컴포넌트 조합 — 프론트엔드 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/projects/{project_id}/composition/status")
async def composition_status(
    project_id: str,
    db: DatabaseAdapter = Depends(get_db),
):
    """프로젝트의 composition 파이프라인 상태 조회."""
    composition_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.state, n.phase
           FROM nodes n WHERE n.project_id=?
             AND n.name IN ('디자인 토큰', '컴포넌트 라이브러리', '컴포넌트 레지스트리',
                            '페이지 레시피', '페이지 조립')
           ORDER BY n.name""",
        (project_id,),
    )

    from engine.composition.registry import CompositionRegistry
    comp_registry = CompositionRegistry(db)

    tokens = await comp_registry.load_tokens(project_id)
    component_names = await comp_registry.get_component_names(project_id)
    recipes = await comp_registry.load_all_recipes(project_id)

    return {
        "nodes": [
            {"id": n["id"], "name": n["name"], "state": n["state"]}
            for n in composition_nodes
        ],
        "registry": {
            "tokens_ready": tokens is not None,
            "tokens_version": tokens.version if tokens else 0,
            "components_count": len(component_names),
            "component_names": component_names,
            "recipes_count": len(recipes),
            "recipe_pages": [r.page_slug for r in recipes],
        },
    }


@app.get("/api/v1/projects/{project_id}/composition/preview/{component_name}")
async def composition_component_preview(
    project_id: str,
    component_name: str,
    db: DatabaseAdapter = Depends(get_db),
):
    """단일 컴포넌트 미리보기 HTML."""
    from fastapi.responses import HTMLResponse
    from engine.composition.registry import CompositionRegistry
    from engine.composition.renderer import CompositionRenderer

    comp_registry = CompositionRegistry(db)
    renderer = CompositionRenderer(comp_registry)

    # 샘플 데이터 (컴포넌트 슬롯 기본값)
    comp = await comp_registry.load_component(project_id, component_name)
    if not comp:
        return HTMLResponse(f"<h1>컴포넌트 없음: {component_name}</h1>", status_code=404)

    sample_data = {}
    for slot_name, slot_def in comp.slots.items():
        if isinstance(slot_def, dict) and "default" in slot_def:
            sample_data[slot_name] = slot_def["default"]
        else:
            sample_data[slot_name] = f"[{slot_name}]"

    html = await renderer.render_preview(project_id, component_name, sample_data)
    return HTMLResponse(html)


@app.post("/api/v1/projects/{project_id}/composition/reassemble")
async def composition_reassemble(
    project_id: str,
    body: dict = None,
    current_user: dict = Depends(get_current_user),
    db: DatabaseAdapter = Depends(get_db),
):
    """수동 재조립 트리거. 토큰/컴포넌트/레시피 수정 후 즉시 결과를 확인할 때 사용."""
    RBAC.require(current_user["role"], Permission.CREATE_PROJECT)

    from engine.composition.registry import CompositionRegistry
    from engine.composition.renderer import CompositionRenderer

    comp_registry = CompositionRegistry(db)
    renderer = CompositionRenderer(comp_registry)

    page_slugs = (body or {}).get("pages")  # None이면 전체
    if page_slugs:
        results = []
        for slug in page_slugs:
            r = await renderer.render_page(project_id, slug)
            results.append(r)
    else:
        results = await renderer.render_all_pages(project_id)

    if not results:
        raise HTTPException(status_code=404, detail="렌더링할 레시피가 없습니다")

    # 조립 노드의 artifact에 저장
    assembly_node = await db.fetchone(
        "SELECT id FROM nodes WHERE project_id=? AND name='페이지 조립'",
        (project_id,),
    )
    if assembly_node:
        now = _now()
        for result in results:
            import hashlib as _hl_ra
            art = await db.fetchone(
                "SELECT id, current_version FROM artifacts WHERE node_id=?",
                (assembly_node["id"],),
            )
            if art:
                new_ver = (art["current_version"] or 0) + 1
                await db.execute(
                    "UPDATE artifacts SET current_version=?, updated_at=? WHERE id=?",
                    (new_ver, now, art["id"]),
                )
                await db.execute(
                    "INSERT INTO artifact_versions (id, artifact_id, version_num, "
                    "storage_path, content_hash, size_bytes, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), art["id"], new_ver, result.html,
                     _hl_ra.sha256(result.html.encode()).hexdigest(),
                     len(result.html), current_user["user_id"], now),
                )

    return {
        "pages": [
            {
                "page_name": r.page_name,
                "page_slug": r.page_slug,
                "content_hash": r.content_hash,
                "components_used": r.components_used,
                "warnings": r.warnings,
            }
            for r in results
        ],
        "total": len(results),
    }


# ---------------------------------------------------------------------------
# 소급 검증 (Retroactive Validation) — 수동 트리거 API
# ---------------------------------------------------------------------------

@app.post("/api/v1/retroactive-validate")
async def retroactive_validate(
    db: DatabaseAdapter = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    body: dict = Body(default={}),
):
    """기존 COMPLETED 산출물을 최신 harness 규칙으로 소급 검증.

    관리자 전용. project_id 지정 시 해당 프로젝트만, 미지정 시 전체 검증.
    """
    RBAC.require(current_user["role"], Permission.MANAGE_SYSTEM)

    project_id = body.get("project_id")

    from engine.skills.qa.retroactive import run_retroactive_validation
    results = await run_retroactive_validation(db, project_id)

    fail_count = sum(1 for r in results if r.get("result") == "FAIL")
    pass_count = sum(1 for r in results if r.get("result") == "PASS")

    return {
        "status": "completed",
        "summary": {
            "total_checks": len(results),
            "pass": pass_count,
            "fail": fail_count,
        },
        "results": results,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Habit Tracker: Test endpoint (임시)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/habits-test")
async def test_habit(body: dict = Body(...)):
    """Test endpoint to verify routing works."""
    return {"received": body, "message": "Test OK"}
