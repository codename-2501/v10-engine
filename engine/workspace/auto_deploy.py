"""
engine/workspace/auto_deploy.py
BUILD 완료 자동 감지 → 코드 추출 → 빌드 → 포트 할당 → 서버 기동.

코어 파일(dag_advancer, state_machine 등) 미접촉.
Outbox 폴링 패턴으로 독립 동작.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.db.adapter import DatabaseAdapter
from engine.workspace.config_gen import (
    _apply_design_tokens,
    _generate_swagger,
)
from engine.workspace.paths import (
    WORKSPACES_ROOT,
    _make_slug,
    _resolve_workspace_path,
    _sanitize_code_for_workspace,
)
from engine.workspace.server_mgmt import (
    _cleanup_processes,
    _detect_stack,
    _install_and_build,
    _port_in_use,
    _start_servers,
    _wait_for_server,
)

logger = logging.getLogger("engine.workspace.auto_deploy")

# 프론트/백엔드 포트 범위
_FE_PORT_BASE = 3000
_BE_PORT_BASE = 4000
_PORT_STEP = 100


class WorkspaceDeployWorker:
    """
    BUILD→VERIFY 게이트 자동승인 후 워크스페이스 배포를 자동화하는 폴링 워커.

    주기적으로 DB를 체크:
      1. BUILD 게이트가 COMPLETED 상태
      2. 아직 workspace_deployments 기록 없음 (= 미배포)
    → 추출 + 빌드 + 포트 할당 + 서버 기동
    """

    POLL_INTERVAL = 10  # 초

    def __init__(self, db: DatabaseAdapter, ai_adapter=None) -> None:
        self._db = db
        self._ai_adapter = ai_adapter
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("workspace_deploy_worker_started")
        # 테이블 생성 (없으면)
        await self._ensure_table()
        _health_cycle = 0
        while self._running:
            try:
                await self._check_and_deploy()
            except Exception as exc:
                logger.error("workspace_deploy_worker_error error=%s", str(exc))
            # [v8+] 배포 후 모니터링: 6사이클(60초)마다 헬스체크 + 자동 재시작
            _health_cycle += 1
            if _health_cycle % 6 == 0:
                try:
                    await self._health_monitor()
                except Exception as exc:
                    logger.error("workspace_health_monitor_error error=%s", str(exc))
            await asyncio.sleep(self.POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        logger.info("workspace_deploy_worker_stopped")

    async def _ensure_table(self) -> None:
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_deployments (
                id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                frontend_port INTEGER,
                backend_port INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDING',
                deploy_step TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(project_id)
            )
        """)
        # 기존 테이블에 컬럼 추가 (이미 있으면 무시)
        # warnings: SHOULD 단계 실패를 JSON 배열로 누적. status=COMPLETED여도
        # 이 필드에 쌓인 항목은 사용자에게 advisory로 노출.
        for col, typedef in [
            ("deploy_step", "TEXT"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("warnings", "TEXT"),
        ]:
            try:
                await self._db.execute(f"ALTER TABLE workspace_deployments ADD COLUMN {col} {typedef}")
            except Exception as exc:
                logger.warning("alter_table_add_column_failed col=%s error=%s", col, exc)

    MAX_AUTO_RESTART = 3

    async def _health_monitor(self) -> None:
        """[v8+] 배포 완료된 서비스 헬스체크 + 다운 감지 시 자동 재시작."""
        rows = await self._db.fetchall(
            """SELECT id, project_id, workspace_path, frontend_port, backend_port
               FROM workspace_deployments WHERE status='COMPLETED'"""
        )
        for row in rows:
            ws = Path(row["workspace_path"]) if row["workspace_path"] else None
            if not ws or not ws.is_dir():
                continue
            for label, port in [("backend", row["backend_port"]), ("frontend", row["frontend_port"])]:
                if not port:
                    continue
                alive = _port_in_use(port)
                if not alive:
                    # 재시작 카운트 확인
                    restart_file = ws / f".restart_count_{label}"
                    count = 0
                    if restart_file.is_file():
                        try:
                            count = int(restart_file.read_text().strip())
                        except Exception as exc:
                            logger.debug("restart_count_read_failed file=%s error=%s", restart_file, exc)
                            count = 0
                    if count >= self.MAX_AUTO_RESTART:
                        logger.error(
                            "health_restart_exhausted project=%s label=%s port=%d count=%d",
                            row["project_id"], label, port, count,
                        )
                        continue
                    # 자동 재시작
                    logger.warning(
                        "health_down_detected project=%s label=%s port=%d — restarting (%d/%d)",
                        row["project_id"], label, port, count + 1, self.MAX_AUTO_RESTART,
                    )
                    restart_file.write_text(str(count + 1))
                    try:
                        # 좀비 프로세스 정리
                        await asyncio.to_thread(subprocess.run, 
                            f"lsof -ti :{port} | xargs kill -9 2>/dev/null || true",
                            shell=True, timeout=5,
                        )
                        stack = _detect_stack(ws)
                        await asyncio.to_thread(
                            _start_servers, ws, stack, row["frontend_port"], row["backend_port"],
                        )
                        await asyncio.to_thread(_wait_for_server, port, timeout=15)
                        logger.info("health_restarted project=%s label=%s port=%d", row["project_id"], label, port)
                    except Exception as exc:
                        logger.error("health_restart_failed project=%s error=%s", row["project_id"], str(exc))
                else:
                    # alive면 재시작 카운트 리셋
                    restart_file = ws / f".restart_count_{label}"
                    if restart_file.is_file():
                        restart_file.unlink(missing_ok=True)

    async def _check_and_deploy(self) -> None:
        """미배포 + 실패 재시도 대상 프로젝트 탐색 → 배포."""
        MAX_RETRIES = 3

        # 0) [v8+] CASCADE 재배포 감지: BUILD 노드가 재완료됐으면 기존 배포를 리셋
        try:
            stale_deploys = await self._db.fetchall("""
                SELECT wd.id AS deploy_id, wd.project_id, wd.completed_at
                FROM workspace_deployments wd
                WHERE wd.status = 'COMPLETED'
                  AND EXISTS (
                      SELECT 1 FROM nodes n
                      WHERE n.project_id = wd.project_id
                        AND n.phase = 'BUILD'
                        AND n.node_type = 'TASK'
                        AND n.state = 'COMPLETED'
                        AND n.completed_at > wd.completed_at
                  )
            """)
            for sd in stale_deploys:
                await self._db.execute(
                    """UPDATE workspace_deployments
                       SET status='PENDING', deploy_step='CASCADE_REDEPLOY',
                           retry_count=0, error_message=NULL
                       WHERE id=?""",
                    (sd["deploy_id"],),
                )
                logger.info(
                    "cascade_redeploy_detected project_id=%s — resetting deployment",
                    sd["project_id"],
                )
        except Exception as exc:
            logger.warning("cascade_redeploy_check_error error=%s", str(exc))

        # 1) 신규 배포 대상 (레코드 없음)
        new_pending = await self._db.fetchall("""
            SELECT DISTINCT
                n.project_id,
                p.engagement_id,
                e.name AS engagement_name
            FROM nodes n
            JOIN projects p ON p.id = n.project_id
            JOIN engagements e ON e.id = p.engagement_id
            WHERE n.node_type = 'GATE'
              AND n.state IN ('COMPLETED', 'AWAITING_APPROVAL')
              AND n.name LIKE '%BUILD%VERIFY%'
              AND n.project_id NOT IN (
                  SELECT project_id FROM workspace_deployments
              )
        """)

        # 2) 실패/재배포 대상 (FAILED 또는 PENDING(cascade 리셋) + retry_count < MAX_RETRIES)
        retry_pending = await self._db.fetchall(f"""
            SELECT wd.id AS deploy_id, wd.project_id, wd.engagement_id,
                   wd.retry_count, wd.workspace_path,
                   e.name AS engagement_name
            FROM workspace_deployments wd
            JOIN engagements e ON e.id = wd.engagement_id
            WHERE wd.status IN ('FAILED', 'PENDING')
              AND wd.retry_count < {MAX_RETRIES}
        """)

        # 신규 배포
        for row in new_pending:
            project_id = row["project_id"]
            engagement_name = row["engagement_name"]
            engagement_id = row["engagement_id"]
            deploy_id = str(uuid.uuid4())

            try:
                await self._db.execute(
                    """INSERT INTO workspace_deployments
                       (id, engagement_id, project_id, workspace_path,
                        status, deploy_step, retry_count, created_at)
                       VALUES (?, ?, ?, '', 'IN_PROGRESS', 'STARTING', 0, ?)""",
                    (deploy_id, engagement_id, project_id, _now()),
                )
            except Exception as exc:
                logger.debug("deploy_insert_failed project=%s error=%s", project_id, exc)
                continue

            await self._run_deploy(deploy_id, project_id, engagement_id, engagement_name, 0)

        # 실패 재시도
        for row in retry_pending:
            deploy_id = row["deploy_id"]
            project_id = row["project_id"]
            engagement_id = row["engagement_id"]
            engagement_name = row["engagement_name"]
            retry_count = row["retry_count"] + 1

            # 기존 프로세스 정리
            ws_path = row.get("workspace_path")
            if ws_path:
                _cleanup_processes(Path(ws_path))

            await self._db.execute(
                """UPDATE workspace_deployments
                   SET status='IN_PROGRESS', deploy_step='RETRYING',
                       retry_count=?, error_message=NULL
                   WHERE id=?""",
                (retry_count, deploy_id),
            )
            logger.info(
                "workspace_deploy_retry project_id=%s attempt=%d/%d",
                project_id, retry_count, MAX_RETRIES,
            )
            await self._run_deploy(deploy_id, project_id, engagement_id, engagement_name, retry_count)

    async def _run_deploy(
        self,
        deploy_id: str,
        project_id: str,
        engagement_id: str,
        engagement_name: str,
        retry_count: int,
    ) -> None:
        """단일 프로젝트 배포 실행 + 결과 처리."""
        logger.info(
            "workspace_deploy_started project_id=%s engagement=%s retry=%d",
            project_id, engagement_name, retry_count,
        )

        try:
            result = await self._deploy_project(
                project_id, engagement_id, engagement_name,
            )
            # 배포 advisory 결과 (quality_advisory 등)를 warnings JSON으로 저장
            import json as _json
            warnings_entries = result.get("warnings", [])
            warnings_json = _json.dumps(warnings_entries, ensure_ascii=False) if warnings_entries else None
            final_step = "DONE_WITH_WARNINGS" if warnings_entries else "DONE"
            await self._db.execute(
                """UPDATE workspace_deployments
                   SET status='COMPLETED', workspace_path=?,
                       frontend_port=?, backend_port=?,
                       deploy_step=?, completed_at=?, warnings=?
                   WHERE id=?""",
                (
                    str(result["workspace_path"]),
                    result["frontend_port"],
                    result["backend_port"],
                    final_step,
                    _now(),
                    warnings_json,
                    deploy_id,
                ),
            )
            logger.info(
                "workspace_deploy_completed project_id=%s fe_port=%s be_port=%s",
                project_id, result["frontend_port"], result["backend_port"],
            )
        except Exception as exc:
            status = "PERMANENTLY_FAILED" if retry_count >= 2 else "FAILED"
            await self._db.execute(
                """UPDATE workspace_deployments
                   SET status=?, error_message=?, deploy_step='ERROR'
                   WHERE id=?""",
                (status, str(exc)[:1000], deploy_id),
            )
            if status == "PERMANENTLY_FAILED":
                logger.error(
                    "workspace_deploy_permanently_failed project_id=%s error=%s",
                    project_id, str(exc),
                )
            else:
                logger.warning(
                    "workspace_deploy_failed project_id=%s retry=%d error=%s",
                    project_id, retry_count, str(exc),
                )

    async def _update_step(self, project_id: str, step: str) -> None:
        """배포 단계 업데이트 (대시보드 실시간 진행 표시용)."""
        await self._db.execute(
            "UPDATE workspace_deployments SET deploy_step=? WHERE project_id=?",
            (step, project_id),
        )

    async def _deploy_project(
        self,
        project_id: str,
        engagement_id: str,
        engagement_name: str,
    ) -> dict:
        """전체 배포 파이프라인 (단계 추적 + 헬스체크 + 조기 중단)."""

        # ── STEP 1: BUILD 산출물 조회 (race condition 방어: 최대 3회 재시도) ──
        # DAGAdvancer가 노드를 COMPLETED로 마킹한 직후 artifact_versions INSERT가
        # 아직 커밋되지 않았을 수 있음 → 짧은 대기 후 재조회
        await self._update_step(project_id, "EXTRACTING")
        artifacts = {}
        for _attempt in range(3):
            artifacts = await self._get_build_artifacts(project_id)
            if artifacts:
                break
            logger.info(
                "workspace_artifacts_not_ready project=%s attempt=%d/3 — waiting",
                project_id, _attempt + 1,
            )
            await asyncio.sleep(2)
        if not artifacts:
            raise RuntimeError("BUILD 산출물이 없습니다 (3회 재시도 후에도 미발견)")

        # ── STEP 2: 워크스페이스 경로 ──
        slug = _make_slug(engagement_name)
        workspace_path = WORKSPACES_ROOT / slug

        # 기존 workspace 재사용: slug의 접두사로 시작하는 디렉토리가 이미 있으면 그걸 사용
        # "명성실버케어센터-디지털-전환-플랫폼" 요청 시 기존 "명성실버케어센터" 발견하면 재사용
        if not workspace_path.is_dir() and WORKSPACES_ROOT.is_dir():
            first_segment = slug.split("-")[0]
            if first_segment:
                for existing in WORKSPACES_ROOT.iterdir():
                    if existing.is_dir() and existing.name.startswith(first_segment):
                        workspace_path = existing
                        slug = existing.name
                        logger.info(
                            "workspace_reuse_existing new_slug=%s existing=%s",
                            _make_slug(engagement_name), existing.name,
                        )
                        break

        workspace_path.mkdir(parents=True, exist_ok=True)

        # 이미 워크스페이스에 코드가 있으면 재추출 스킵 (수동 수정 보호)
        ports_file = workspace_path / "ports.json"
        fe_dir = workspace_path / "frontend"
        be_dir = workspace_path / "backend"
        workspace_has_code = (
            (fe_dir.is_dir() and any(fe_dir.rglob("*.tsx")))
            or (be_dir.is_dir() and (any(be_dir.rglob("*.ts")) or any(f for f in be_dir.rglob("*.py") if ".venv" not in str(f))))
        )

        if ports_file.is_file():
            existing_ports = json.loads(ports_file.read_text())
            fe_port = existing_ports.get("frontend", 0)
            be_port = existing_ports.get("backend", 0)
            if fe_port and be_port and _port_in_use(fe_port) and _port_in_use(be_port):
                logger.info("workspace_already_running path=%s — skipping", workspace_path)
                return {
                    "workspace_path": workspace_path,
                    "frontend_port": fe_port,
                    "backend_port": be_port,
                    "stack": _detect_stack(workspace_path),
                }

        # ── STEP 3: 코드 추출 (이미 코드가 있으면 스킵) ──
        if workspace_has_code:
            logger.info("workspace_code_exists path=%s — skipping extraction", workspace_path)
        else:
            await asyncio.to_thread(
                _extract_code_to_workspace, artifacts, workspace_path, engagement_name,
            )

        # ── STEP 4: 스택 감지 ──
        await self._update_step(project_id, "DETECTING_STACK")
        stack = _detect_stack(workspace_path)
        logger.info("workspace_stack_detected stack=%s", stack)

        # ── STEP 5: 포트 할당 ──
        await self._update_step(project_id, "ALLOCATING_PORTS")
        fe_port, be_port = await self._allocate_ports()
        api_docs_path = "/api-docs" if stack.get("backend") == "express" else "/docs"
        ports_file.write_text(json.dumps({
            "frontend": fe_port, "backend": be_port, "api_docs_path": api_docs_path,
        }, indent=2))

        # ── STEP 5-1: [v8+] DESIGN 디자인 토큰 → globals.css 강제 적용 ──
        await self._update_step(project_id, "DESIGN_TOKENS")
        try:
            token_applied = await asyncio.to_thread(
                _apply_design_tokens, workspace_path, project_id, self._db,
            )
            if token_applied:
                logger.info("workspace_design_tokens_applied project=%s", project_id)
        except Exception as exc:
            logger.warning("workspace_design_tokens_error error=%s", str(exc))

        # ── STEP 6a: 범용 이슈 사전 수정 (SQLite 서비스코드, req.params, rate limiter, cookie) ──
        await self._update_step(project_id, "PRE_FIX")
        try:
            from engine.workspace.verify_and_fix import fix_universal_issues
            pre_fixes = await asyncio.to_thread(fix_universal_issues, workspace_path, stack)
            if pre_fixes:
                logger.info("workspace_pre_fixes count=%d", len(pre_fixes))
        except Exception as exc:
            logger.warning("workspace_pre_fix_error error=%s", str(exc))

        # ── STEP 6b: UI 완성도 자동 수정 (죽은 버튼, 누락 페이지, 네비게이션) ──
        await self._update_step(project_id, "UI_FIX")
        try:
            from engine.workspace.ui_completeness import fix_ui_completeness
            ui_report = await asyncio.to_thread(fix_ui_completeness, workspace_path, stack)
            if ui_report and ui_report.get("fixes"):
                logger.info("workspace_ui_fixes count=%d", len(ui_report["fixes"]))
        except Exception as exc:
            logger.warning("workspace_ui_fix_error error=%s", str(exc))

        # ── STEP 6b-2: Next.js 라우트 슬러그 충돌 검증 (UI_FIX 이후 안전망) ──
        # ui_completeness에 가드를 넣었지만, 과거 생성 산출물/외부 요인 등으로
        # 중복이 남아있을 수 있어 배포 진입 전 프로그래매틱 교정 한 번 더.
        try:
            from engine.workspace.programmatic_verify import (
                _auto_fix_route_slug_collisions,
            )
            fe_dir = workspace_path / "frontend"
            if fe_dir.is_dir():
                slug_fixes = await asyncio.to_thread(
                    _auto_fix_route_slug_collisions, fe_dir,
                )
                if slug_fixes:
                    logger.info(
                        "workspace_route_slug_pre_fixes count=%d",
                        len(slug_fixes),
                    )
        except Exception as exc:
            logger.warning("workspace_route_slug_pre_fix_error error=%s", str(exc))

        # ── STEP 6b: 의존성 설치 + 빌드 ──
        await self._update_step(project_id, "INSTALLING")
        await asyncio.to_thread(_install_and_build, workspace_path, stack, fe_port, be_port)

        # ── STEP 7: 빌드-수정 루프 (패턴 + AI 폴백) ──
        await self._update_step(project_id, "BUILD_FIX")
        try:
            from engine.workspace.verify_and_fix import build_fix_loop
            fix_result = await asyncio.to_thread(
                build_fix_loop, workspace_path, stack, ai_adapter=self._ai_adapter,
            )
            if not fix_result:
                fix_result = {"backend": None, "frontend": None}
            be_result = fix_result.get("backend") or {}
            fe_result = fix_result.get("frontend") or {}
            be_ok = be_result.get("success", True)
            fe_ok = fe_result.get("success", True)
            if not be_ok:
                raise RuntimeError(f"백엔드 빌드 실패 (자동 수정 불가): {be_result.get('errors', [])[:3]}")
        except ImportError:
            logger.debug("verify_and_fix module not available — skipping")

        # ── STEP 7-1: [v8+] Mock→API 자동 연결 ──
        await self._update_step(project_id, "API_CONNECT")
        try:
            from engine.workspace.api_connector import connect_frontend_to_backend
            _ports = {"frontend": fe_port, "backend": be_port}
            api_conn = await asyncio.to_thread(connect_frontend_to_backend, workspace_path, _ports)
            if api_conn and api_conn.get("connected", 0) > 0:
                logger.info("workspace_api_connected count=%d", api_conn["connected"])
        except Exception as exc:
            logger.warning("workspace_api_connect_error error=%s", str(exc))

        # ── STEP 8: 시드 데이터 ──
        await self._update_step(project_id, "SEEDING")
        try:
            from engine.workspace.seed_generator import generate_seed
            await asyncio.to_thread(generate_seed, workspace_path, stack)
        except Exception as exc:
            logger.warning("workspace_seed_failed error=%s", str(exc))

        # ── STEP 9: 서버 기동 ──
        await self._update_step(project_id, "STARTING_SERVERS")
        await asyncio.to_thread(_start_servers, workspace_path, stack, fe_port, be_port)

        # ── STEP 10: 헬스체크 (로그 패턴 + HTTP 폴링) ──
        # 포트 리슨만으로는 크래시 중인 Next.js도 통과시키므로 로그 기반
        # ready/fatal 마커를 병행. 타임아웃/치명 에러 시 _wait_for_server가
        # RAISE → 배포 FAILED 승격.
        await self._update_step(project_id, "HEALTH_CHECK")
        be_log = workspace_path / "backend.log"
        fe_log = workspace_path / "frontend.log"
        fe_ready = (
            "Ready in",
            "compiled successfully",
            "ready started server",
            "Local:        http://localhost",
        )
        fe_fatal = (
            "same slug name",
            "repeat within a single dynamic path",
            "Error: Cannot find module",
            "EADDRINUSE",
            "ELIFECYCLE",
            "Failed to compile",
        )
        be_ready = (
            "Uvicorn running",
            "Application startup complete",
            "Started server process",
            "ready - started server",
            "listening on",
        )
        be_fatal = (
            "Traceback (most recent call last)",
            "EADDRINUSE",
            "ImportError",
            "ModuleNotFoundError",
        )
        await asyncio.to_thread(
            _wait_for_server, be_port, 30, be_log, be_ready, be_fatal,
        )
        await asyncio.to_thread(
            _wait_for_server, fe_port, 45, fe_log, fe_ready, fe_fatal,
        )

        # ── STEP 11: 데이터 계약 검증 ──
        await self._update_step(project_id, "CONTRACT_VERIFY")
        try:
            from engine.workspace.contract_verify import verify_api_contracts
            contracts = await asyncio.to_thread(verify_api_contracts, workspace_path, stack, be_port)
            if contracts:
                logger.warning("workspace_contract_mismatches count=%d", len(contracts))
        except Exception as exc:
            logger.warning("workspace_contract_check_error error=%s", str(exc))

        # ── STEP 11-1: [v8+] 기존 워크스페이스 매니페스트 import 크로스체크 ──
        await self._update_step(project_id, "MANIFEST_CHECK")
        try:
            manifest_issues = await asyncio.to_thread(
                _manifest_cross_check_existing, workspace_path
            )
            if manifest_issues:
                logger.warning("workspace_manifest_issues count=%d", len(manifest_issues))
        except Exception as exc:
            logger.warning("workspace_manifest_check_error error=%s", str(exc))

        # ── STEP 11-2: [v8+] 프론트/백 필드명 불일치 자동 수정 ──
        await self._update_step(project_id, "FIELD_MISMATCH_FIX")
        try:
            field_fixes = await asyncio.to_thread(
                _fix_field_name_mismatch, workspace_path
            )
            if field_fixes:
                logger.info("workspace_field_fixes count=%d", len(field_fixes))
        except Exception as exc:
            logger.warning("workspace_field_fix_error error=%s", str(exc))

        # ── STEP 11-3: [v8+] Swagger/OpenAPI 자동 생성 ──
        await self._update_step(project_id, "SWAGGER_GEN")
        try:
            swagger_count = await asyncio.to_thread(
                _generate_swagger, workspace_path, stack, be_port
            )
            if swagger_count:
                logger.info("workspace_swagger_generated endpoints=%d", swagger_count)
        except Exception as exc:
            logger.warning("workspace_swagger_gen_error error=%s", str(exc))

        # ── STEP 12: E2E 테스트 ──
        await self._update_step(project_id, "E2E_TESTING")
        test_result = {"passed": 0, "total": 0, "success": False}
        try:
            from engine.workspace.test_generator import generate_and_run_tests
            test_result = await asyncio.to_thread(
                generate_and_run_tests, workspace_path, stack, fe_port, be_port,
            )
            logger.info(
                "workspace_e2e_result project=%s passed=%s/%s",
                engagement_name, test_result["passed"], test_result["total"],
            )
        except Exception as exc:
            logger.warning("workspace_e2e_skipped error=%s", str(exc))

        # ── STEP 13: [v8+] 런타임 E2E 검증 (HTTP 기반) ──
        await self._update_step(project_id, "E2E_VALIDATE")
        e2e_validation = {"passed": True, "summary": "skipped"}
        try:
            from engine.workspace.e2e_validator import run_e2e_validation
            _ports = {"frontend": fe_port, "backend": be_port}
            e2e_validation = await asyncio.to_thread(run_e2e_validation, workspace_path, _ports)
            if e2e_validation.get("passed"):
                logger.info("workspace_e2e_validate_ok summary=%s", e2e_validation.get("summary"))
            else:
                logger.warning(
                    "workspace_e2e_validate_issues summary=%s failed=%d",
                    e2e_validation.get("summary"), e2e_validation.get("failed_pages", 0),
                )
        except Exception as exc:
            logger.warning("workspace_e2e_validate_error error=%s", str(exc))

        # ── STEP 13-1: [v8+] 품질 advisory — 금지 패턴(보라 그라디언트·쿠키커터·
        # placeholder 카피) 자동 감지. 배포 차단하지 않고 warnings에 누적.
        await self._update_step(project_id, "QUALITY_ADVISORY")
        quality_warnings: list[str] = []
        try:
            from engine.workspace.quality_advisory import scan_workspace_quality
            q_report = await asyncio.to_thread(scan_workspace_quality, workspace_path)
            if q_report.findings:
                quality_warnings = q_report.to_warning_entries()
                logger.info(
                    "quality_advisory project=%s score=%.2f summary=%s",
                    engagement_name, q_report.score, q_report.summary,
                )
        except Exception as exc:
            logger.warning("quality_advisory_error error=%s", str(exc))

        # ── STEP 13-2: [v8+] 접근성 advisory (정적 WCAG 검사) ──
        a11y_warnings: list[str] = []
        try:
            from engine.workspace.a11y_audit import scan_workspace_a11y
            a_report = await asyncio.to_thread(scan_workspace_a11y, workspace_path)
            if a_report.findings:
                # 상위 20건만 warnings에 (너무 많으면 시그널 희석)
                a11y_warnings = a_report.to_warning_entries()[:20]
                logger.info(
                    "a11y_advisory project=%s files=%d findings=%d",
                    engagement_name, a_report.files_scanned, len(a_report.findings),
                )
        except Exception as exc:
            logger.warning("a11y_advisory_error error=%s", str(exc))

        # ── STEP 14: [v8+] 시각적 비교 검증 (advisory — 배포 차단하지 않음) ──
        await self._update_step(project_id, "VISUAL_CHECK")
        visual_result: dict = {}
        try:
            from engine.skills.qa.visual_check import run_visual_checks
            visual_result = await asyncio.to_thread(
                run_visual_checks,
                workspace_path,
                {"frontend_port": fe_port, "backend_port": be_port},
                project_id,
            )
            if not visual_result.get("pass", True):
                logger.warning(
                    "visual_check_issues project=%s issues=%d score=%.2f",
                    engagement_name,
                    len(visual_result.get("issues", [])),
                    visual_result.get("score", 0),
                )
            else:
                logger.info(
                    "visual_check_pass project=%s pages=%d score=%.2f",
                    engagement_name,
                    visual_result.get("pages_checked", 0),
                    visual_result.get("score", 1.0),
                )
        except Exception as exc:
            logger.warning("visual_check_error project=%s error=%s", engagement_name, str(exc))

        # ── STEP 15: PLAYWRIGHT_FULL_TEST (A-grade verification) ──
        await self._update_step(project_id, "PLAYWRIGHT_FULL_TEST")
        pw_results: dict = {}
        try:
            from engine.testing.playwright_bridge import run_playwright_tests
            _pw_ports = {"frontend": fe_port, "backend": be_port}
            pw_results = await run_playwright_tests(workspace_path, project_id, _pw_ports)
            if not pw_results.get("pass"):
                logger.warning(
                    "playwright_tests_failed project=%s score=%.1f",
                    engagement_name,
                    pw_results.get("summary", {}).get("score", 0),
                )
            else:
                logger.info(
                    "playwright_tests_pass project=%s score=%.1f",
                    engagement_name,
                    pw_results.get("summary", {}).get("score", 0),
                )
        except Exception as exc:
            logger.warning("playwright_skipped error=%s", str(exc))

        # SHOULD 단계에서 누적된 advisory 경고들을 배포 메타에 포함.
        # _check_and_deploy가 이걸 DB warnings 컬럼에 JSON으로 저장.
        advisory_warnings: list[str] = []
        advisory_warnings.extend(quality_warnings)
        advisory_warnings.extend(a11y_warnings)
        if visual_result and not visual_result.get("pass", True):
            advisory_warnings.append(
                f"visual_check[score={visual_result.get('score', 0):.2f}]: "
                f"{len(visual_result.get('issues', []))} issues"
            )
        if e2e_validation and not e2e_validation.get("passed", True):
            advisory_warnings.append(
                f"e2e_validate[failed={e2e_validation.get('failed_pages', 0)}]: "
                f"{e2e_validation.get('summary', '?')}"
            )
        if pw_results and not pw_results.get("pass", True):
            advisory_warnings.append(
                f"playwright[score={pw_results.get('summary', {}).get('score', 0):.1f}]: "
                f"below threshold"
            )

        return {
            "workspace_path": workspace_path,
            "frontend_port": fe_port,
            "backend_port": be_port,
            "stack": stack,
            "test_result": test_result,
            "e2e_validation": e2e_validation,
            "visual_result": visual_result,
            "playwright": pw_results,
            "warnings": advisory_warnings,
        }

    async def _get_build_artifacts(self, project_id: str) -> dict[str, str]:
        """프로젝트의 BUILD 산출물을 DB에서 조회."""
        rows = await self._db.fetchall("""
            SELECT n.name, av.storage_path AS content
            FROM nodes n
            JOIN artifacts a ON a.node_id = n.id
            JOIN artifact_versions av ON av.artifact_id = a.id
            WHERE n.project_id = ?
              AND n.phase = 'BUILD'
              AND n.node_type = 'TASK'
              AND n.state = 'COMPLETED'
            ORDER BY av.version_num DESC
        """, (project_id,))

        artifacts = {}
        for r in rows:
            name = r.get("name") or ""
            content = r.get("content") or ""
            if name and content and name not in artifacts:  # 최신 버전만, 빈 콘텐츠 스킵
                artifacts[name] = content
        return artifacts

    async def _allocate_ports(self) -> tuple[int, int]:
        """사용중인 포트 확인 → 다음 사용 가능 포트 할당."""
        existing = await self._db.fetchall(
            "SELECT frontend_port, backend_port FROM workspace_deployments WHERE status='COMPLETED'"
        )
        used_fe = {r["frontend_port"] for r in existing if r["frontend_port"]}
        used_be = {r["backend_port"] for r in existing if r["backend_port"]}

        fe_port = _FE_PORT_BASE
        while fe_port in used_fe or _port_in_use(fe_port):
            fe_port += _PORT_STEP

        be_port = _BE_PORT_BASE
        while be_port in used_be or _port_in_use(be_port):
            be_port += _PORT_STEP

        return fe_port, be_port


# ---------------------------------------------------------------------------
# 순수 함수들 (asyncio.to_thread에서 실행)
# ---------------------------------------------------------------------------


# _NODE_BUILTINS, _SKIP_PACKAGES, _is_node_builtin_or_skip, _npm_safe_name,
# _make_slug  →  moved to engine.workspace.paths


def _extract_code_to_workspace(
    artifacts: dict[str, str],
    workspace_path: Path,
    project_name: str,
) -> None:
    """산출물에서 코드 추출 → 파일 생성.

    2가지 형식 지원:
      1. // FILE: 구분자 (프로그래매틱 생성 코드) → 직접 파일 쓰기
      2. 마크다운 코드 블록 (AI 생성 코드) → _parse_code_blocks 파싱
    """
    total_files = 0

    for artifact_name, content in artifacts.items():
        if not content or not content.strip():
            continue

        # ── 프로그래매틱 코드 감지: // FILE: 또는 /* FILE: 태그가 있으면 직접 분할 쓰기 ──
        if re.search(r'(?://|/\*)\s*FILE:', content) and content.strip().startswith(("//", "/*", "'use")):
            written = _write_file_tagged_content(content, workspace_path)
            total_files += written
            logger.info(
                "workspace_file_tagged_write artifact=%s files=%d",
                artifact_name, written,
            )
            continue

        # ── 마크다운 코드 블록 파싱 (AI 생성 or 레거시) ──
        blocks = _parse_code_blocks(content)
        if not blocks:
            continue

        lower_name = artifact_name.lower()
        if "프론트" in lower_name or "frontend" in lower_name:
            _write_frontend_files(workspace_path / "frontend", blocks)
        elif "백엔드" in lower_name or "backend" in lower_name or "api" in lower_name:
            _write_backend_files(workspace_path / "backend", blocks)
        elif "db" in lower_name or "스키마" in lower_name or "마이그레이션" in lower_name:
            _write_db_files(workspace_path / "db", blocks)
        else:
            # 분류 불가 → 파일 경로에서 추론
            _write_auto_categorized(workspace_path, blocks)
        total_files += len(blocks)

    # import 크로스체크 (마크다운 방식에서만 — file-tagged는 이미 완전)
    logger.info("workspace_extraction_done path=%s total_files=%d", workspace_path, total_files)



# _sanitize_code_for_workspace, _fix_import_paths  →  moved to engine.workspace.paths


def _write_file_tagged_content(content: str, workspace_path: Path) -> int:
    """// FILE: path 태그로 구분된 프로그래매틱 코드 → 실제 파일 쓰기.

    지원 경로 패턴:
      // FILE: src/app/layout.tsx     → frontend/src/app/layout.tsx
      // FILE: src/server.ts          → backend/src/server.ts
      // FILE: src/routes/users.ts    → backend/src/routes/users.ts
      // FILE: prisma/schema.prisma   → backend/prisma/schema.prisma
      // FILE: prisma/migrations/...  → backend/prisma/migrations/...
      // FILE: src/components/X.tsx   → frontend/src/components/X.tsx
      // FILE: src/pages/X.tsx        → frontend/src/pages/X.tsx
      // FILE: src/app/globals.css    → frontend/src/app/globals.css
    """
    # // FILE: 또는 /* FILE: 태그로 분할 (CSS 주석 스타일 포함)
    parts = re.split(r'^(?://|/\*)\s*FILE:\s*(\S+?)(?:\s*\*/)?$', content, flags=re.MULTILINE)
    # parts: ['preamble', 'path1', 'code1', 'path2', 'code2', ...]

    written = 0
    i = 1
    while i < len(parts) - 1:
        filepath = parts[i].strip()
        code = parts[i + 1]
        i += 2

        if not filepath or not code.strip():
            continue

        # 자동 정리 (마크다운 잔여물, Python 문법 등)
        code_clean = _sanitize_code_for_workspace(code, filepath)
        if not code_clean:
            continue

        target = _resolve_workspace_path(filepath, workspace_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code_clean, encoding="utf-8")
        written += 1

    return written



# _resolve_workspace_path  →  moved to engine.workspace.paths


def _write_auto_categorized(workspace_path: Path, blocks: list[dict]) -> None:
    """분류 불가 블록 → 파일 경로에서 카테고리 자동 추론하여 쓰기."""
    for block in blocks:
        fp = block["filepath"]
        target = _resolve_workspace_path(fp, workspace_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(block["code"], encoding="utf-8")


def _cross_check_imports(
    all_blocks: dict[str, list[dict]],
    extracted_paths: set[str],
) -> list[dict]:
    """매니페스트 imports vs 실제 추출 파일 크로스체크. 누락 로컬 import 반환."""
    missing = []
    for category, blocks in all_blocks.items():
        for block in blocks:
            for imp in block.get("imports", []):
                # 외부 패키지는 스킵 (로컬 상대경로만 체크)
                if not imp.startswith("./") and not imp.startswith("../") and not imp.startswith("@/"):
                    continue
                # @/components/Foo → src/components/Foo 변환
                resolved = imp
                if resolved.startswith("@/"):
                    resolved = "src/" + resolved[2:]
                # 확장자 없으면 .ts/.tsx 시도
                candidates = [resolved]
                if not any(resolved.endswith(ext) for ext in (".ts", ".tsx", ".js", ".jsx", ".css")):
                    candidates.extend([resolved + ".ts", resolved + ".tsx", resolved + "/index.ts", resolved + "/index.tsx"])
                if not any(c in extracted_paths for c in candidates):
                    missing.append({"file": block["filepath"], "missing_import": imp})
    return missing


def _parse_code_blocks(content: str) -> list[dict]:
    """매니페스트 우선 파싱. 매니페스트 없으면 // FILE: 태그 → 레거시 정규식 폴백."""
    # ── 1단계: FILE_MANIFEST JSON 파싱 ──
    manifest = _parse_file_manifest(content)

    # ── 2단계: 코드 블록 추출 (// FILE: 태그 우선) ──
    blocks_by_path: dict[str, dict] = {}

    # 파일 경로 문자 클래스 — Next.js (group)/[param], 일반 경로 모두 지원
    PATH_CHARS = r'[\w/.\-\(\)\[\]]'

    # 패턴 A: // FILE: path 태그가 있는 코드 블록 (신규 방식)
    pattern_file_tag = re.compile(
        rf'```(\w+)\n\s*//\s*FILE:\s*({PATH_CHARS}+\.\w+)\s*\n([\s\S]*?)```',
    )
    for lang, filepath, code in pattern_file_tag.findall(content):
        fp = filepath.strip()
        blocks_by_path[fp] = {
            "lang": lang,
            "filepath": fp,
            "code": code.strip(),
            "imports": [],
        }

    # 패턴 B: 주석에 파일 경로 (// src/app/(main)/page.tsx 등)
    if not blocks_by_path:
        pattern_comment = re.compile(
            rf'```(\w+)\n\s*(?://|#|/\*)\s*({PATH_CHARS}+\.\w+).*?\n([\s\S]*?)```',
        )
        for lang, filepath, code in pattern_comment.findall(content):
            fp = filepath.strip()
            if fp not in blocks_by_path:
                blocks_by_path[fp] = {
                    "lang": lang,
                    "filepath": fp,
                    "code": code.strip(),
                    "imports": [],
                }

    # 패턴 C: 헤딩 + 코드 첫줄에서 경로 추출
    if not blocks_by_path:
        pattern_heading = re.compile(
            r'(?:#{1,4}\s*(?:\d+\.?\s*)?)?(?:\*\*)?([^\n*`]+?)(?:\*\*)?\s*\n+```(\w+)\n([\s\S]*?)```',
        )
        for heading, lang, code in pattern_heading.findall(content):
            heading = heading.strip().rstrip(":")
            file_match = re.search(rf'\(({PATH_CHARS}+\.\w+)\)', heading)
            if file_match:
                fp = file_match.group(1).strip()
            else:
                first_line = code.strip().split("\n")[0] if code.strip() else ""
                path_match = re.search(rf'(?://|#|/\*)\s*({PATH_CHARS}+\.\w+)', first_line)
                if path_match:
                    fp = path_match.group(1).strip()
                else:
                    continue
            if fp not in blocks_by_path:
                blocks_by_path[fp] = {
                    "lang": lang,
                    "filepath": fp,
                    "code": code.strip(),
                    "imports": [],
                }

    # ── 3단계: 매니페스트 imports 정보를 블록에 병합 ──
    if manifest:
        manifest_paths = {f["path"] for f in manifest}
        for entry in manifest:
            path = entry["path"]
            if path in blocks_by_path:
                blocks_by_path[path]["imports"] = entry.get("imports", [])
            else:
                # 매니페스트에는 있지만 코드 블록이 없음 → 경고
                logger.warning("manifest_missing_code path=%s", path)

        # 매니페스트에 없는 코드 블록 경고 (AI가 매니페스트를 빠뜨림)
        for fp in blocks_by_path:
            if fp not in manifest_paths:
                logger.warning("code_block_not_in_manifest path=%s", fp)

    return list(blocks_by_path.values())


def _parse_file_manifest(content: str) -> list[dict] | None:
    """<!-- FILE_MANIFEST {...} --> 블록에서 매니페스트 파싱. 없으면 None."""
    match = re.search(
        r'<!--\s*FILE_MANIFEST\s*\n(\{[\s\S]*?\})\s*-->',
        content,
    )
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
        files = data.get("files", [])
        if not isinstance(files, list):
            logger.warning("manifest_invalid_format not_a_list")
            return None
        # 최소 검증: path 필드 존재
        valid = [f for f in files if isinstance(f, dict) and "path" in f]
        if not valid:
            logger.warning("manifest_empty")
            return None
        logger.info("manifest_parsed files=%d", len(valid))
        return valid
    except json.JSONDecodeError as exc:
        logger.warning("manifest_json_error error=%s", str(exc))
        return None


def _write_frontend_files(base: Path, blocks: list[dict]) -> None:
    """프론트엔드 코드 블록 → 파일 생성."""
    for block in blocks:
        filepath = block["filepath"]
        if not filepath.startswith("src/") and not filepath.startswith("public/"):
            filepath = "src/" + filepath
        code = _sanitize_code_for_workspace(block["code"], filepath)
        if not code:
            continue
        target = base / filepath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")


def _write_backend_files(base: Path, blocks: list[dict]) -> None:
    """백엔드 코드 블록 → 파일 생성."""
    for block in blocks:
        filepath = block["filepath"]
        code = _sanitize_code_for_workspace(block["code"], filepath)
        if not code:
            continue
        target = base / filepath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")


def _write_db_files(base: Path, blocks: list[dict]) -> None:
    """DB 마이그레이션 파일 생성."""
    migrations_dir = base / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    for i, block in enumerate(blocks, 1):
        filename = block.get("filepath", f"{i:03d}_migration.sql")
        if "/" in filename:
            filename = filename.split("/")[-1]
        target = migrations_dir / filename
        target.write_text(block["code"], encoding="utf-8")



# _detect_stack, _install_and_build, _start_servers, _run_cmd,
# _port_in_use, _wait_for_server, _cleanup_processes
# → moved to engine.workspace.server_mgmt


def _now() -> str:
    """UTC ISO timestamp. (engine/skills/utils._now과 동일 — workspace 패키지 독립성 유지)"""
    return datetime.now(timezone.utc).isoformat()


# _apply_design_tokens → moved to engine.workspace.config_gen


# ============================================================
# [v8+] 매니페스트 import 크로스체크 (기존 워크스페이스용)
# ============================================================

def _manifest_cross_check_existing(workspace_path: Path) -> list[dict]:
    """이미 코드가 존재하는 워크스페이스에서 import 누락 감지."""
    issues = []
    for subdir in ("frontend", "backend"):
        src_dir = workspace_path / subdir / "src"
        if not src_dir.is_dir():
            continue
        all_files = set()
        for f in src_dir.rglob("*"):
            if f.is_file() and f.suffix in (".ts", ".tsx", ".js", ".jsx"):
                all_files.add(str(f.relative_to(workspace_path / subdir)))
        # 각 파일의 import 검사
        for f in src_dir.rglob("*.ts*"):
            if "node_modules" in str(f) or ".next" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("file_read_failed path=%s error=%s", f, exc)
                continue
            for m in re.finditer(r"""(?:import|from)\s+['"](@/[^'"]+|\.\.?/[^'"]+)['"]""", content):
                imp = m.group(1)
                resolved = imp.replace("@/", "src/") if imp.startswith("@/") else str(
                    (f.parent / imp).resolve().relative_to((workspace_path / subdir).resolve())
                ) if imp.startswith(".") else imp
                candidates = [resolved, resolved + ".ts", resolved + ".tsx",
                              resolved + "/index.ts", resolved + "/index.tsx"]
                if not any(c in all_files for c in candidates):
                    issues.append({"file": str(f.relative_to(workspace_path)), "missing": imp})
    if issues:
        logger.warning("manifest_cross_check count=%d first=%s", len(issues), issues[:3])
    return issues


# ============================================================
# [v8+] 프론트/백 필드명 불일치 자동 수정
# ============================================================

def _fix_field_name_mismatch(workspace_path: Path) -> list[str]:
    """서버 응답 필드(snake_case) vs 프론트 타입(camelCase) 불일치 감지 + 자동 치환."""
    fixes = []
    be = workspace_path / "backend"
    fe = workspace_path / "frontend"
    if not be.is_dir() or not fe.is_dir():
        return fixes

    # 1) 서버 필드 추출: res.json / res.send에서 키 추출
    server_fields = set()
    for ts_file in be.rglob("*.ts"):
        if "node_modules" in str(ts_file):
            continue
        try:
            code = ts_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", ts_file, exc)
            continue
        for m in re.finditer(r'res\.(?:json|send)\s*\(\s*\{([^}]{1,500})\}', code):
            for km in re.finditer(r'(\w+)\s*[,:]', m.group(1)):
                name = km.group(1)
                if len(name) > 1 and name not in ("true", "false", "null", "data", "success", "message", "error"):
                    server_fields.add(name)

    # 2) 프론트 타입 필드 추출
    front_fields = set()
    for ts_file in fe.rglob("*.ts"):
        if "node_modules" in str(ts_file) or ".next" in str(ts_file):
            continue
        if not any(kw in ts_file.name.lower() for kw in ("type", "interface", "model", "dto")):
            continue
        try:
            code = ts_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", ts_file, exc)
            continue
        for m in re.finditer(r'^\s+(\w+)\s*[?:]', code, re.MULTILINE):
            front_fields.add(m.group(1))

    # 3) 불일치 감지 + 치환
    mismatches = []
    for sf in server_fields:
        camel = re.sub(r'_([a-z])', lambda m: m.group(1).upper(), sf)
        if camel != sf and camel in front_fields:
            mismatches.append((sf, camel))  # server: snake, front: camel
    for sf in server_fields:
        snake = re.sub(r'([A-Z])', r'_\1', sf).lower()
        if snake != sf and snake in front_fields:
            mismatches.append((sf, snake))

    if not mismatches:
        return fixes

    # 서버 기준으로 프론트 코드 치환
    for server_name, front_name in mismatches:
        for tsx_file in fe.rglob("*.tsx"):
            if "node_modules" in str(tsx_file) or ".next" in str(tsx_file):
                continue
            try:
                content = tsx_file.read_text(encoding="utf-8")
                original = content
                escaped = re.escape(front_name)
                # 프로퍼티 접근: .frontName → .serverName
                content = re.sub(rf'(?<=\.)({escaped})(?=\s*[;,)\]}}:?\n])', server_name, content)
                # 구조분해: { frontName } → { serverName }
                content = re.sub(rf'(?<=[\{{,\s])\b{escaped}\b(?=\s*[,}}:])', server_name, content)
                # interface 필드: frontName?: → serverName?:
                content = re.sub(rf'^(\s+){escaped}(\s*[?:])', rf'\1{server_name}\2', content, flags=re.MULTILINE)
                if content != original:
                    tsx_file.write_text(content, encoding="utf-8")
                    fixes.append(f"field_fix: {tsx_file.name} ({front_name}→{server_name})")
            except Exception as exc:
                logger.debug("field_fix_failed file=%s error=%s", tsx_file, exc)
                continue

    logger.info("field_mismatch_fixed mismatches=%d files=%d", len(mismatches), len(fixes))
    return fixes


# _generate_swagger → moved to engine.workspace.config_gen
