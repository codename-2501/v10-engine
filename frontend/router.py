"""
frontend/router.py
Jinja2 + HTMX Dashboard 라우터.
FastAPI app에 마운트:
    from frontend.router import register_dashboard_routes
    register_dashboard_routes(app, get_db, get_current_user)
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from engine.db.adapter import DatabaseAdapter

# 템플릿 경로
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)


PHASE_ORDER = ["DEFINE", "DESIGN", "BUILD", "VERIFY", "DELIVER"]
PHASE_LABEL = {
    "DEFINE": "정의", "DESIGN": "설계",
    "BUILD": "구현", "VERIFY": "검증", "DELIVER": "납품·운영",
}


def _phase_state(ph_data: dict) -> str:
    """페이즈 상태 결정 (타임라인 표시용)."""
    if not ph_data["total"]:
        return "pending"
    states = [n["state"] for n in ph_data["tasks"] + ph_data["qa"] if n["state"] != "SKIPPED"]
    if not states:
        return "completed"
    if all(s == "COMPLETED" for s in states):
        return "completed"
    if any(s == "FAILED" for s in states):
        return "failed"
    if any(s in ("AWAITING_APPROVAL", "NEEDS_HUMAN") for s in states):
        return "awaiting"
    if any(s == "IN_PROGRESS" for s in states):
        return "in_progress"
    if any(s == "READY" for s in states):
        return "in_progress"
    if any(s == "BLOCKED" for s in states):
        return "blocked"
    return "pending"


async def _load_engagement_projects_data(db, engagement_id: str) -> dict:
    """engagement_detail / live_fragment 공통 데이터 로딩.

    Returns dict with keys: projects_data, overall_pct, total_nodes, completed_nodes.
    """
    from collections import defaultdict

    node_rows = await db.fetchall(
        """SELECT n.id, n.name, n.state, n.node_type, n.phase,
                  n.qa_pair_node_id, n.task_pair_node_id, n.priority,
                  p.id as project_id, p.name as project_name,
                  COALESCE(tu.total_tokens, 0) as total_tokens
           FROM nodes n
           JOIN dags d ON d.id = n.dag_id
           JOIN projects p ON p.id = d.project_id
           LEFT JOIN (
               SELECT node_id, SUM(input_tokens + output_tokens) as total_tokens
               FROM agent_token_usage GROUP BY node_id
           ) tu ON tu.node_id = n.id
           WHERE p.engagement_id = ?
           ORDER BY p.id,
                    CASE n.phase
                        WHEN 'DEFINE' THEN 1 WHEN 'DESIGN' THEN 2
                        WHEN 'BUILD' THEN 3 WHEN 'VERIFY' THEN 4
                        WHEN 'DELIVER' THEN 5 ELSE 99 END,
                    n.node_type DESC, n.priority, n.created_at""",
        (engagement_id,),
    )

    all_projects = await db.fetchall(
        "SELECT id, name, status, phase FROM projects WHERE engagement_id=?",
        (engagement_id,),
    )

    projects_data = {}
    for p in all_projects:
        projects_data[p["id"]] = {
            "id": p["id"], "name": p["name"], "status": p["status"],
            "phase": p["phase"], "phases": {},
        }

    # BLOCKED 노드 차단 사유 + 모든 노드의 상위 참조 문서 목록
    # blocking_map: "← X 대기" (state=BLOCKED 전용 표시)
    # upstream_refs_map: 노드가 참조하는 상위 TASK 문서들 (완료 후에도 표시)
    # v8 DAG 구조: TASK ← QA ← TASK(이전 단계 문서). QA 부모는 task_pair로
    # resolve해서 '진짜 참조 문서'인 TASK를 찾아야 의미있는 '기반 문서' 표시 가능.
    blocking_map: dict = {}
    upstream_refs_map: dict = {}
    if node_rows:
        all_ids = [n["id"] for n in node_rows]
        placeholders = ",".join("?" for _ in all_ids)
        # 전체 edge + 부모 노드 정보 + QA의 task_pair 정보까지 한 번에
        all_dep_rows = await db.fetchall(
            f"""SELECT e.to_node_id,
                       n2.id AS parent_id,
                       n2.name AS parent_name,
                       n2.state AS parent_state,
                       n2.node_type AS parent_type,
                       n2.task_pair_node_id AS parent_task_pair,
                       tp.id AS tp_id,
                       tp.name AS tp_name,
                       tp.state AS tp_state
                FROM edges e
                JOIN nodes n2 ON n2.id = e.from_node_id
                LEFT JOIN nodes tp ON tp.id = n2.task_pair_node_id
                WHERE e.to_node_id IN ({placeholders}) AND e.is_active = 1
                ORDER BY e.to_node_id""",
            tuple(all_ids),
        )
        _blocking = defaultdict(list)
        _refs = defaultdict(list)
        _seen: dict = defaultdict(set)  # 중복 참조 제거용 (to_id, parent_id)
        for dr in all_dep_rows:
            to_id = dr["to_node_id"]
            pstate = dr["parent_state"]
            pname = dr["parent_name"]
            ptype = dr["parent_type"]
            # BLOCKED 사유: 미완료 dependency (node_type 무관)
            if pstate not in ("COMPLETED", "SKIPPED"):
                _blocking[to_id].append(pname)

            # 상위 참조 문서 추적
            if ptype == "TASK":
                # 직접 TASK 부모 (드물지만 존재)
                key = dr["parent_id"]
                if key not in _seen[to_id]:
                    _seen[to_id].add(key)
                    _refs[to_id].append({
                        "id": dr["parent_id"],
                        "name": pname,
                        "state": pstate,
                    })
            elif ptype == "QA" and dr["tp_id"]:
                # QA 부모 → task_pair 로 resolve (진짜 문서)
                key = dr["tp_id"]
                if key not in _seen[to_id]:
                    _seen[to_id].add(key)
                    _refs[to_id].append({
                        "id": dr["tp_id"],
                        "name": dr["tp_name"],
                        "state": dr["tp_state"],
                    })

        # BLOCKED 표시 (기존 동작 유지)
        for nid, names in _blocking.items():
            if len(names) == 1:
                blocking_map[nid] = f"← {names[0][:30]} 대기"
            else:
                blocking_map[nid] = f"← {len(names)}건 대기"

        # 상위 참조 문서 맵 (완료 후에도 유지)
        upstream_refs_map = dict(_refs)

    for n in node_rows:
        pid = n["project_id"]
        if pid not in projects_data:
            projects_data[pid] = {"id": pid, "name": n["project_name"], "phases": {}}
        phase = n["phase"]
        if phase not in projects_data[pid]["phases"]:
            projects_data[pid]["phases"][phase] = {"tasks": [], "qa": [], "gates": [], "total": 0, "completed": 0}
        ph = projects_data[pid]["phases"][phase]
        node_d = dict(n)
        node_d["blocking_reason"] = blocking_map.get(n["id"], "")
        # 상위 참조 문서 목록 — 모든 상태에서 표시 (완료 후에도 근거 확인 가능)
        node_d["upstream_refs"] = upstream_refs_map.get(n["id"], [])
        if n["node_type"] == "GATE":
            # 미리보기 URL 첨부 (BUILD GATE — 승인 전후 모두 표시)
            node_d["preview_url"] = None
            node_d["deploy_status"] = None
            node_d["deploy_step"] = None
            node_d["deploy_warnings"] = []
            node_d["verification_badges"] = []
            if "BUILD" in (n.get("name") or ""):
                try:
                    wd = await db.fetchone(
                        """SELECT frontend_port, status, deploy_step, warnings
                             FROM workspace_deployments
                             WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
                        (pid,),
                    )
                    if wd:
                        if wd.get("frontend_port"):
                            node_d["preview_url"] = f"http://localhost:{wd['frontend_port']}"
                        node_d["deploy_status"] = wd.get("status")
                        node_d["deploy_step"] = wd.get("deploy_step")
                        warnings_list: list = []
                        if wd.get("warnings"):
                            try:
                                import json as _json
                                warnings_list = _json.loads(wd["warnings"])
                            except Exception:
                                warnings_list = []
                        node_d["deploy_warnings"] = warnings_list

                        # ── 런타임 검증 뱃지 계산 (카테고리별) ──
                        # 배포가 COMPLETED면 기본적으로 build/runtime 통과
                        # warnings 카테고리별로 세부 상태 판정
                        _dep_status = (wd.get("status") or "").upper()
                        _dep_done = _dep_status == "COMPLETED"

                        def _count_by_prefix(prefix: str) -> int:
                            return sum(
                                1 for w in warnings_list
                                if isinstance(w, str) and w.startswith(prefix)
                            )

                        badges: list[dict] = []
                        # 1) 빌드/기동 — deploy 완료면 통과
                        if _dep_done:
                            badges.append({"label": "빌드", "state": "pass", "detail": "next build OK"})
                            badges.append({"label": "기동", "state": "pass", "detail": "서버 Ready"})
                        elif _dep_status == "FAILED":
                            badges.append({"label": "빌드/기동", "state": "fail", "detail": "배포 실패"})
                        elif _dep_status in ("PENDING", "IN_PROGRESS"):
                            badges.append({"label": "빌드/기동", "state": "progress", "detail": wd.get("deploy_step") or "진행 중"})

                        # 2) E2E (HTTP 200 스모크)
                        _e2e_fail = _count_by_prefix("e2e_validate[")
                        if _dep_done and _e2e_fail == 0:
                            badges.append({"label": "E2E", "state": "pass", "detail": "HTTP 200"})
                        elif _e2e_fail > 0:
                            badges.append({"label": "E2E", "state": "warn", "detail": f"{_e2e_fail}건 경고"})

                        # 3) Playwright (UI 클릭 자동화)
                        _pw_fail = _count_by_prefix("playwright[")
                        if _dep_done and _pw_fail == 0:
                            badges.append({"label": "UI테스트", "state": "pass", "detail": "Playwright"})
                        elif _pw_fail > 0:
                            badges.append({"label": "UI테스트", "state": "warn", "detail": f"{_pw_fail}건 미달"})

                        # 4) 시각 검증
                        _vis_fail = _count_by_prefix("visual_check[")
                        if _dep_done and _vis_fail == 0:
                            badges.append({"label": "시각", "state": "pass", "detail": "디자인 매치"})
                        elif _vis_fail > 0:
                            badges.append({"label": "시각", "state": "warn", "detail": f"{_vis_fail}건 불일치"})

                        # 5) 접근성 (a11y)
                        _a11y_count = _count_by_prefix("a11y[")
                        if _a11y_count == 0 and _dep_done:
                            badges.append({"label": "접근성", "state": "pass", "detail": "WCAG OK"})
                        elif _a11y_count > 0:
                            badges.append({"label": "접근성", "state": "warn", "detail": f"{_a11y_count}건 findings"})

                        # 6) 품질 (보라 그라디언트·쿠키커터·placeholder)
                        _q_count = _count_by_prefix("quality_advisory[")
                        if _q_count == 0 and _dep_done:
                            badges.append({"label": "품질", "state": "pass", "detail": "금지 패턴 없음"})
                        elif _q_count > 0:
                            badges.append({"label": "품질", "state": "warn", "detail": f"{_q_count}건 findings"})

                        node_d["verification_badges"] = badges
                except Exception:
                    pass
            ph["gates"].append(node_d)
        elif n["node_type"] == "QA":
            ph["qa"].append(node_d)
        else:
            ph["tasks"].append(node_d)
        if n["node_type"] != "GATE" and n["state"] != "SKIPPED":
            ph["total"] += 1
            if n["state"] == "COMPLETED":
                ph["completed"] += 1

    # 위상 정렬용 깊이 계산
    edge_rows_all = await db.fetchall(
        """SELECT e.from_node_id, e.to_node_id FROM edges e
           WHERE e.dag_id IN (SELECT d.id FROM dags d JOIN projects p ON d.project_id=p.id WHERE p.engagement_id=?)
             AND e.is_active = 1""",
        (engagement_id,),
    )
    parent_map = {}
    for er in edge_rows_all:
        parent_map.setdefault(er["to_node_id"], []).append(er["from_node_id"])

    node_depth = {}
    def _depth(nid, visited=None):
        if nid in node_depth:
            return node_depth[nid]
        if visited is None:
            visited = set()
        if nid in visited:
            return 0
        visited.add(nid)
        parents = parent_map.get(nid, [])
        node_depth[nid] = 0 if not parents else 1 + max(_depth(p, visited) for p in parents)
        return node_depth[nid]
    for n in node_rows:
        _depth(n["id"])

    for pid, pd in projects_data.items():
        pd["total"] = sum(ph["total"] for ph in pd["phases"].values())
        pd["completed"] = sum(ph["completed"] for ph in pd["phases"].values())
        pd["pct"] = round(pd["completed"] / pd["total"] * 100) if pd["total"] else 0
        pd["phase_list"] = []
        for ph_name in PHASE_ORDER:
            if ph_name in pd["phases"]:
                ph = pd["phases"][ph_name]
                ph["name"] = ph_name
                ph["label"] = PHASE_LABEL.get(ph_name, ph_name)
                ph["state"] = _phase_state(ph)
                ph["pct"] = round(ph["completed"] / ph["total"] * 100) if ph["total"] else 0
                ph["tasks"].sort(key=lambda t: (1 if t["state"] == "SKIPPED" else 0, node_depth.get(t["id"], 0), t.get("priority", 3)))
                ph["qa"].sort(key=lambda q: (1 if q["state"] == "SKIPPED" else 0, node_depth.get(q.get("task_pair_node_id", ""), 0), q.get("priority", 3)))
                pd["phase_list"].append(ph)

    total_nodes = sum(pd["total"] for pd in projects_data.values())
    completed_nodes = sum(pd["completed"] for pd in projects_data.values())
    overall_pct = round(completed_nodes / total_nodes * 100) if total_nodes else 0

    return {
        "projects_data": list(projects_data.values()),
        "overall_pct": overall_pct,
        "total_nodes": total_nodes,
        "completed_nodes": completed_nodes,
    }


def register_dashboard_routes(app, get_db_func, get_current_user_func) -> None:
    """FastAPI app에 대시보드 라우트 + 정적 파일 마운트."""

    # 정적 파일 (htmx.min.js, d3.min.js)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # ── 메인 대시보드 ─────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(
        request: Request,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        # 시스템 상태
        nodes = await db.fetchone(
            """SELECT
               SUM(CASE WHEN state='IN_PROGRESS'   THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN state='NEEDS_HUMAN'   THEN 1 ELSE 0 END) AS needs_human,
               SUM(CASE WHEN state='SUSPENDED'     THEN 1 ELSE 0 END) AS suspended
               FROM nodes"""
        )
        # ShutdownManager는 app.state에서 가져옴 (server.py 참조)
        shutting_down = False
        try:
            from api.server import state as app_state
            shutting_down = app_state.shutdown_manager.is_shutting_down
        except Exception:
            pass

        status = {
            "active_agents": (nodes["active"] or 0) if nodes else 0,
            "needs_human": (nodes["needs_human"] or 0) if nodes else 0,
            "suspended": (nodes["suspended"] or 0) if nodes else 0,
            "shutting_down": shutting_down,
        }

        # 인게이지먼트 목록 + 런타임 검증 뱃지
        engagements_rows = await db.fetchall(
            "SELECT id, name, client_name, status, priority FROM engagements "
            "ORDER BY priority, created_at DESC LIMIT 20"
        )

        # 각 engagement의 가장 최근 deploy 상태 + warnings 조회 → 대시보드 뱃지 계산
        engagements: list[dict] = []
        for e in engagements_rows:
            e_dict = dict(e)
            e_dict["verification_badges"] = []
            try:
                wd = await db.fetchone(
                    """SELECT wd.status, wd.deploy_step, wd.warnings
                         FROM workspace_deployments wd
                         JOIN projects p ON p.id = wd.project_id
                         WHERE p.engagement_id = ?
                         ORDER BY wd.created_at DESC LIMIT 1""",
                    (e_dict["id"],),
                )
                if wd:
                    import json as _json
                    warnings_list = []
                    if wd.get("warnings"):
                        try:
                            warnings_list = _json.loads(wd["warnings"])
                        except Exception:
                            warnings_list = []

                    _dep_status = (wd.get("status") or "").upper()
                    _dep_done = _dep_status == "COMPLETED"

                    def _c(prefix: str) -> int:
                        return sum(
                            1 for w in warnings_list
                            if isinstance(w, str) and w.startswith(prefix)
                        )

                    badges: list[dict] = []
                    if _dep_done:
                        badges.append({"label": "빌드", "state": "pass"})
                        badges.append({"label": "기동", "state": "pass"})
                    elif _dep_status == "FAILED" or _dep_status == "PERMANENTLY_FAILED":
                        badges.append({"label": "빌드/기동", "state": "fail"})
                    elif _dep_status in ("PENDING", "IN_PROGRESS"):
                        badges.append({"label": "배포", "state": "progress"})

                    _e2e = _c("e2e_validate[")
                    if _dep_done and _e2e == 0:
                        badges.append({"label": "E2E", "state": "pass"})
                    elif _e2e > 0:
                        badges.append({"label": "E2E", "state": "warn", "count": _e2e})

                    _pw = _c("playwright[")
                    if _dep_done and _pw == 0:
                        badges.append({"label": "UI테스트", "state": "pass"})
                    elif _pw > 0:
                        badges.append({"label": "UI테스트", "state": "warn", "count": _pw})

                    _vis = _c("visual_check[")
                    if _dep_done and _vis == 0:
                        badges.append({"label": "시각", "state": "pass"})
                    elif _vis > 0:
                        badges.append({"label": "시각", "state": "warn", "count": _vis})

                    _a11y = _c("a11y[")
                    if _dep_done and _a11y == 0:
                        badges.append({"label": "접근성", "state": "pass"})
                    elif _a11y > 0:
                        badges.append({"label": "접근성", "state": "warn", "count": _a11y})

                    _q = _c("quality_advisory[")
                    if _dep_done and _q == 0:
                        badges.append({"label": "품질", "state": "pass"})
                    elif _q > 0:
                        badges.append({"label": "품질", "state": "warn", "count": _q})

                    e_dict["verification_badges"] = badges
                    e_dict["deploy_status"] = _dep_status
            except Exception:
                pass
            engagements.append(e_dict)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "active_page": "home",
                "status": status,
                "engagements": engagements,
            },
        )

    # ── 인게이지먼트 목록 ─────────────────────────────────────────────────

    @app.get("/engagements", response_class=HTMLResponse)
    async def engagements_list(
        request: Request,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        rows = await db.fetchall(
            """SELECT id, name, client_name, status, priority, created_at
               FROM engagements ORDER BY created_at DESC LIMIT 100"""
        )
        stats = await db.fetchone(
            """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN status='INTAKE'    THEN 1 ELSE 0 END) AS intake,
               SUM(CASE WHEN status='ACTIVE'    THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN status='PAUSED'    THEN 1 ELSE 0 END) AS paused,
               SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN status IN ('ARCHIVED','FORCE_CLOSED') THEN 1 ELSE 0 END) AS closed
               FROM engagements"""
        )
        return templates.TemplateResponse(
            "engagements_list.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagements": [dict(r) for r in rows],
                "stats": dict(stats) if stats else {},
            },
        )

    # ── 인게이지먼트 상세 ─────────────────────────────────────────────────

    @app.get("/engagements/{engagement_id}", response_class=HTMLResponse)
    async def engagement_detail(
        request: Request,
        engagement_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        engagement = await db.fetchone(
            "SELECT * FROM engagements WHERE id=?", (engagement_id,)
        )
        if not engagement:
            return HTMLResponse(
                "<html><body style='background:#111;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;'>"
                "<div style='text-align:center;'><h1>404</h1><p>인게이지먼트를 찾을 수 없습니다</p>"
                "<a href='/intake/submissions' style='color:#60a5fa;'>접수 관리로 돌아가기</a></div></body></html>",
                status_code=404,
            )

        common = await _load_engagement_projects_data(db, engagement_id)

        # Plan 추출 (global_context JSON 안에 _project_plan)
        _plan_data = {}
        _plan_md = ""
        try:
            _gc = engagement.get("global_context") or "{}"
            _gc_dict = json.loads(_gc) if isinstance(_gc, str) else _gc
            _plan_data = _gc_dict.get("_project_plan", {})
            if _plan_data:
                from engine.intake.project_planner import plan_to_markdown
                _plan_md = plan_to_markdown(_plan_data)
        except Exception:
            pass

        # Escalations
        escalations = await db.fetchall(
            """SELECT e.id, e.node_id, e.description AS reason, e.created_at, p.id AS project_id
               FROM escalations e
               JOIN nodes n ON n.id = e.node_id
               JOIN dags d ON d.id = n.dag_id
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? AND e.resolved_at IS NULL
               ORDER BY e.created_at DESC""",
            (engagement_id,),
        )

        # Gate approvals pending
        gates = await db.fetchall(
            """SELECT n.id, n.name, n.phase, p.id AS project_id, p.name AS project_name
               FROM nodes n
               JOIN dags d ON d.id = n.dag_id
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? AND n.state = 'AWAITING_APPROVAL'""",
            (engagement_id,),
        )

        # DAG 상태 조회 (시작/일시정지 버튼 표시용)
        dag_row = await db.fetchone(
            """SELECT d.status FROM dags d
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? LIMIT 1""",
            (engagement_id,),
        )
        dag_status = dag_row["status"] if dag_row else "PENDING"

        # 현재 실행 중인 노드 (에이전트 상태 표시용)
        active_nodes = await db.fetchall(
            """SELECT n.name, n.phase, n.state
               FROM nodes n
               JOIN dags d ON d.id = n.dag_id
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? AND n.state IN ('IN_PROGRESS', 'READY')
               ORDER BY n.state DESC, n.priority
               LIMIT 10""",
            (engagement_id,),
        )

        # 상태별 카운트 (필터 배지용)
        state_counts_rows = await db.fetchall(
            """SELECT n.state, COUNT(*) as cnt
               FROM nodes n
               JOIN dags d ON d.id = n.dag_id
               JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? AND n.node_type != 'GATE'
               GROUP BY n.state""",
            (engagement_id,),
        )
        state_counts = {r["state"]: r["cnt"] for r in state_counts_rows}

        return templates.TemplateResponse(
            "engagement_detail.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagement": dict(engagement),
                **common,
                "escalations": [dict(r) for r in escalations],
                "gates": [dict(r) for r in gates],
                "dag_status": dag_status,
                "active_nodes": [dict(r) for r in active_nodes],
                "state_counts": state_counts,
                "project_plan": _plan_data,
                "project_plan_md": _plan_md,
            },
        )

    @app.get("/engagements/{engagement_id}/live", response_class=HTMLResponse)
    async def engagement_live_fragment(
        request: Request,
        engagement_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """HTMX 부분 교체용 — 동적 콘텐츠만 반환."""
        import asyncio as _aio
        engagement = await db.fetchone(
            "SELECT * FROM engagements WHERE id=?", (engagement_id,)
        )
        if not engagement:
            return HTMLResponse("<div>인게이지먼트 없음</div>", status_code=404)

        common = await _load_engagement_projects_data(db, engagement_id)

        escalations = await db.fetchall(
            """SELECT e.id, e.node_id, e.description AS reason, e.created_at
               FROM escalations e JOIN nodes n ON n.id = e.node_id
               JOIN dags d ON d.id = n.dag_id JOIN projects p ON p.id = d.project_id
               WHERE p.engagement_id = ? AND e.resolved_at IS NULL
               ORDER BY e.created_at DESC""",
            (engagement_id,),
        )
        dag_row = await db.fetchone(
            "SELECT d.status FROM dags d JOIN projects p ON p.id=d.project_id WHERE p.engagement_id=? LIMIT 1",
            (engagement_id,),
        )
        dag_status = dag_row["status"] if dag_row else "PENDING"
        active_nodes = await db.fetchall(
            """SELECT n.name, n.phase, n.state FROM nodes n
               JOIN dags d ON d.id=n.dag_id JOIN projects p ON p.id=d.project_id
               WHERE p.engagement_id=? AND n.state IN ('IN_PROGRESS','READY')
               ORDER BY n.state DESC, n.priority LIMIT 10""",
            (engagement_id,),
        )

        # 워크스페이스 미리보기 URL 조회 (ports.json 기반 + 포트 활성 체크)
        preview_url = None
        try:
            import json as _json_live
            _ws_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspaces")
            _projects_data = common.get("projects_data", [])
            for proj in (_projects_data if isinstance(_projects_data, list) else _projects_data.values()):
                _pname = proj.get("name", "")
                if not _pname or not os.path.isdir(_ws_root):
                    continue
                for _d in os.listdir(_ws_root):
                    if _pname in _d or _d in _pname:
                        _pf = os.path.join(_ws_root, _d, "ports.json")
                        if os.path.isfile(_pf):
                            with open(_pf) as _fp:
                                _fe_port = _json_live.load(_fp).get("frontend", 0)
                            if _fe_port:
                                try:
                                    _, w = await _aio.wait_for(
                                        _aio.open_connection("127.0.0.1", _fe_port), timeout=0.5,
                                    )
                                    w.close()
                                    await w.wait_closed()
                                    preview_url = f"http://localhost:{_fe_port}"
                                except (OSError, _aio.TimeoutError):
                                    pass
                        if preview_url:
                            break
                if preview_url:
                    break
        except Exception as _exc:
            import logging
            logging.getLogger("frontend.router").warning("preview_url_detect_error: %s", _exc)

        return templates.TemplateResponse(
            "engagement_detail_live.html",
            {
                "request": request,
                "engagement": dict(engagement),
                **common,
                "escalations": [dict(r) for r in escalations],
                "dag_status": dag_status,
                "active_nodes": [dict(r) for r in active_nodes],
                "preview_url": preview_url,
            },
        )

    # ── DAG 시각화 ────────────────────────────────────────────────────────

    @app.get("/engagements/{engagement_id}/dag", response_class=HTMLResponse)
    async def dag_view(
        request: Request,
        engagement_id: str,
        project_id: Optional[str] = None,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        engagement = await db.fetchone(
            "SELECT id, name FROM engagements WHERE id=?", (engagement_id,)
        )
        if not engagement:
            raise HTTPException(status_code=404, detail="인게이지먼트 없음")

        # project_id 미지정 시 첫 번째 프로젝트 사용
        if not project_id:
            first_proj = await db.fetchone(
                "SELECT id FROM projects WHERE engagement_id=? ORDER BY created_at LIMIT 1",
                (engagement_id,),
            )
            if first_proj:
                project_id = first_proj["id"]

        nodes: list[dict] = []
        edges: list[dict] = []

        if project_id:
            dag = await db.fetchone(
                "SELECT id FROM dags WHERE project_id=? LIMIT 1", (project_id,)
            )
            if dag:
                dag_id = dag["id"]
                node_rows = await db.fetchall(
                    """SELECT id, name, state, phase, node_type, priority
                       FROM nodes WHERE dag_id=?
                       ORDER BY CASE phase
                                    WHEN 'DEFINE' THEN 1 WHEN 'DESIGN' THEN 2
                                    WHEN 'BUILD' THEN 3 WHEN 'VERIFY' THEN 4
                                    WHEN 'DELIVER' THEN 5 ELSE 99 END,
                                priority""",
                    (dag_id,),
                )
                nodes = [dict(r) for r in node_rows]

                edge_rows = await db.fetchall(
                    "SELECT from_node_id, to_node_id FROM edges WHERE dag_id=? AND is_active=1",
                    (dag_id,),
                )
                edges = [dict(r) for r in edge_rows]

        return templates.TemplateResponse(
            "dag_view.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagement": dict(engagement),
                "nodes": nodes,
                "edges": edges,
                "nodes_json": json.dumps(nodes, ensure_ascii=False),
                "edges_json": json.dumps(edges, ensure_ascii=False),
            },
        )

    @app.get("/api/v1/nodes/{node_id}/activity")
    async def node_activity(
        node_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """노드 실시간 활동 — logs/server.out tail 에서 해당 node 이벤트 추출.

        반환:
        - current_action: "지금 뭘 하는지" 자연어 (추론)
        - recent_events: 최근 15개 로그 이벤트
        - last_activity_seconds: 마지막 활동 후 경과 시간
        """
        import os, re as _re, time
        from datetime import datetime

        node_prefix = node_id[:8]
        log_path = os.path.join(os.getcwd(), "logs", "server.out")

        recent_events: list[dict] = []
        current_action: str = ""
        last_activity_ts: float | None = None

        # 로그 파일 tail 200KB 만 읽어서 node prefix 필터
        try:
            if os.path.exists(log_path):
                with open(log_path, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 200_000))
                    tail = f.read().decode("utf-8", errors="ignore")
                for line in reversed(tail.splitlines()):
                    if node_prefix not in line:
                        continue
                    # 주요 이벤트 패턴만 추출
                    event_match = _re.search(
                        r"(chunked_\w+|chunk_items_\w+|harness_\w+|qa_\w+|"
                        r"model_\w+|node_\w+|retry_\w+|cascade_\w+|"
                        r"output_size_\w+|budget_\w+|stall_\w+|verdict_\w+)",
                        line,
                    )
                    if event_match:
                        event_name = event_match.group(1)
                        # 타임스탬프 추출 (ISO 또는 text)
                        ts_match = _re.search(r"\d{4}-\d{2}-\d{2}T[\d:\.]+", line)
                        ts_str = ts_match.group(0) if ts_match else ""
                        # 뒷부분 요약 (최대 120자)
                        summary = line.strip()[-200:] if len(line) > 200 else line.strip()
                        # logger JSON 라인 처리: event·positional_args 파싱 시도
                        if '"event":' in line:
                            try:
                                import json as _j
                                obj = _j.loads(line)
                                summary = str(obj.get("event", summary))[:160]
                            except Exception:
                                pass
                        recent_events.append({
                            "event": event_name,
                            "ts": ts_str,
                            "summary": summary[:200],
                        })
                        if len(recent_events) >= 15:
                            break
        except Exception:
            pass

        # 마지막 활동 시각
        if recent_events and recent_events[0].get("ts"):
            try:
                ts = recent_events[0]["ts"]
                dt = datetime.fromisoformat(ts.rstrip("Z"))
                last_activity_ts = dt.timestamp()
            except Exception:
                last_activity_ts = None

        # current_action 추론 (가장 최근 이벤트 기반)
        if recent_events:
            latest = recent_events[0]
            ev = latest["event"]
            if ev == "chunked_json_item":
                m = _re.search(r"item=(\S+)\s+(ok|failed)", latest["summary"])
                if m:
                    status = m.group(2)
                    item = m.group(1)
                    current_action = (
                        f"✓ 청크 아이템 생성 완료: {item}" if status == "ok"
                        else f"✗ 청크 아이템 실패: {item}"
                    )
            elif ev == "chunked_html_item":
                m = _re.search(r"item=(\S+)\s+ok", latest["summary"])
                if m:
                    current_action = f"✓ HTML 섹션 생성: {m.group(1)}"
            elif ev == "chunked_doc_section_truncated":
                current_action = "⚠ 섹션 절단 — 자동 확장 재시도 중"
            elif ev == "chunked_doc_outline_ids":
                current_action = "📋 Outline ID 리스트 확정 (다음 섹션 생성 준비)"
            elif ev == "chunked_doc_dispatch":
                current_action = "🚀 대형 문서 분할 생성 시작"
            elif ev == "chunk_items_extracted":
                current_action = "📋 Upstream 에서 아이템 list 추출 완료"
            elif ev == "harness_json_pass":
                current_action = "✓ JSON 구조 검증 통과"
            elif ev == "harness_html_pass":
                current_action = "✓ HTML 구조 검증 통과"
            elif ev == "harness_document_pass":
                current_action = "✓ 문서 구조 검증 통과"
            elif ev == "harness_document_fail":
                current_action = "✗ 문서 구조 검증 실패 — 재시도"
            elif ev == "qa_score_pass":
                m = _re.search(r"score=(\d+)", latest["summary"])
                if m:
                    current_action = f"✓ QA PASS (score {m.group(1)})"
            elif ev == "qa_partial_patch":
                current_action = "⚠ 부분 패치 발동 — 실패 부분 재생성 예정"
            elif ev == "verdict_extracted_missing_sections":
                current_action = "🔍 QA 지적 누락 섹션 추출 완료"
            elif ev == "harness_supreme_override":
                current_action = "🛡 S6 Harness-Supreme 오버라이드 → PASS 승격"
            elif ev == "model_router":
                current_action = "🤖 모델 라우팅 중"
            elif ev == "retry_min_floor":
                current_action = "🔁 재시도 — max_tokens 확장"
            elif ev == "output_size_preempt":
                current_action = "🔁 출력 크기 사전 확장 재호출"
            elif ev == "transient_retry":
                m = _re.search(r"reason=(\w+).*attempt=(\d+)", latest["summary"])
                if m:
                    current_action = f"⏳ 일시 오류 재시도 ({m.group(2)}/3) — {m.group(1)}"
                else:
                    current_action = "⏳ 일시 오류 재시도"
            elif ev == "stall_limit_exceeded":
                current_action = "🚫 stall 한도 도달 → SUSPENDED"
            elif ev == "node_token_limit":
                current_action = "🚫 노드 토큰 한도 초과 → SUSPENDED"
            elif ev.startswith("cascade"):
                current_action = "🌊 Cascade 전파 진행"
            else:
                current_action = f"🔧 {ev}"

        return {
            "node_id": node_id,
            "current_action": current_action,
            "recent_events": recent_events,
            "last_activity_seconds": (
                int(time.time() - last_activity_ts) if last_activity_ts else None
            ),
        }

    @app.get("/api/v1/nodes/{node_id}/progress")
    async def node_progress(
        node_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """노드 상세 진행 상황 — Flow 뷰 사이드 패널용.

        반환:
        - 노드 메타 (name, state, retry, stall, node_type, phase)
        - task_snapshot: chunked 진행률 (completed/total items, 진행 중 아이템)
        - QA verdict: description JSON (score, categories, issues 체크리스트)
        - 최근 이벤트 (event_counts 테이블, 있으면)
        """
        row = await db.fetchone(
            """SELECT id, name, node_type, phase, state,
                      retry_count, stall_count,
                      task_snapshot, description, failure_reasons,
                      invalidation_pending, invalidation_source_id,
                      task_pair_node_id, qa_pair_node_id,
                      last_heartbeat, updated_at
               FROM nodes WHERE id=?""",
            (node_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="node not found")
        d = dict(row)

        # task_snapshot 파싱 (chunked_json_items·chunked_html_items·chunked_document)
        snap_parsed = None
        if d.get("task_snapshot"):
            try:
                snap_parsed = json.loads(d["task_snapshot"])
            except Exception:
                pass

        # description (QA verdict) 파싱
        verdict_parsed = None
        if d.get("description"):
            raw = d["description"]
            if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
                try:
                    verdict_parsed = json.loads(raw)
                except Exception:
                    pass

        # failure_reasons 파싱
        failures = []
        if d.get("failure_reasons"):
            try:
                failures = json.loads(d["failure_reasons"])
            except Exception:
                failures = []

        # 진행률 계산 (snap 기반)
        progress = None
        if isinstance(snap_parsed, dict):
            stype = snap_parsed.get("type")
            total = snap_parsed.get("total_count") or len(snap_parsed.get("sections") or snap_parsed.get("completed_items") or [])
            completed = snap_parsed.get("completed_count") or 0
            if stype == "chunked_json_items" or stype == "chunked_html_items":
                items_done = list((snap_parsed.get("completed_items") or {}).keys())
                progress = {
                    "kind": stype, "completed": len(items_done),
                    "total": total or len(items_done),
                    "items_done": items_done[:50],
                }
            elif stype == "chunked_document":
                sec_done = list((snap_parsed.get("sections") or {}).keys())
                progress = {
                    "kind": "chunked_document",
                    "completed": len(sec_done),
                    "total": snap_parsed.get("total_count") or len(sec_done),
                    "items_done": sec_done,
                }

        # 최근 이벤트 (event_counts 테이블 있으면)
        events: list[dict] = []
        try:
            ev_rows = await db.fetchall(
                """SELECT event_name, count, last_at
                   FROM event_counts
                   WHERE last_payload LIKE ? OR event_name LIKE ?
                   ORDER BY last_at DESC LIMIT 10""",
                (f"%{node_id[:8]}%", f"%{node_id[:8]}%"),
            )
            events = [dict(r) for r in ev_rows]
        except Exception:
            events = []

        return {
            "id": d["id"],
            "name": d["name"],
            "node_type": d["node_type"],
            "phase": d["phase"],
            "state": d["state"],
            "retry_count": d.get("retry_count") or 0,
            "stall_count": d.get("stall_count") or 0,
            "last_heartbeat": d.get("last_heartbeat"),
            "updated_at": d.get("updated_at"),
            "invalidation_pending": bool(d.get("invalidation_pending")),
            "invalidation_source_id": d.get("invalidation_source_id"),
            "task_pair_node_id": d.get("task_pair_node_id"),
            "qa_pair_node_id": d.get("qa_pair_node_id"),
            "progress": progress,
            "verdict": verdict_parsed,
            "failures": failures[-5:] if failures else [],
            "recent_events": events,
        }

    @app.get("/engagements/{engagement_id}/flow", response_class=HTMLResponse)
    async def dag_flow_view(
        request: Request,
        engagement_id: str,
        project_id: Optional[str] = None,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """React Flow 기반 인터랙티브 DAG 시각화 — n8n 스타일.

        실시간 상태 업데이트·cascade 관계·노드 클릭 상세 패널 제공.
        """
        engagement = await db.fetchone(
            "SELECT id, name FROM engagements WHERE id=?", (engagement_id,)
        )
        if not engagement:
            raise HTTPException(status_code=404, detail="인게이지먼트 없음")

        if not project_id:
            first_proj = await db.fetchone(
                "SELECT id FROM projects WHERE engagement_id=? ORDER BY created_at LIMIT 1",
                (engagement_id,),
            )
            if first_proj:
                project_id = first_proj["id"]

        nodes: list[dict] = []
        edges: list[dict] = []

        if project_id:
            dag = await db.fetchone(
                "SELECT id FROM dags WHERE project_id=? LIMIT 1", (project_id,)
            )
            if dag:
                dag_id = dag["id"]
                # 상세 필드까지 포함 (cascade · retry · stall)
                node_rows = await db.fetchall(
                    """SELECT id, name, state, phase, node_type, priority,
                              retry_count, stall_count,
                              invalidation_pending, invalidation_source_id,
                              task_pair_node_id, qa_pair_node_id
                       FROM nodes WHERE dag_id=?
                       ORDER BY CASE phase
                                    WHEN 'DEFINE' THEN 1 WHEN 'DESIGN' THEN 2
                                    WHEN 'BUILD' THEN 3 WHEN 'VERIFY' THEN 4
                                    WHEN 'DELIVER' THEN 5 ELSE 99 END,
                                priority""",
                    (dag_id,),
                )
                nodes = [dict(r) for r in node_rows]
                edge_rows = await db.fetchall(
                    "SELECT from_node_id, to_node_id FROM edges WHERE dag_id=? AND is_active=1",
                    (dag_id,),
                )
                edges = [dict(r) for r in edge_rows]

        return templates.TemplateResponse(
            "dag_flow.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagement": dict(engagement),
                "nodes_json": json.dumps(nodes, ensure_ascii=False),
                "edges_json": json.dumps(edges, ensure_ascii=False),
            },
        )

    # ── Stage 13: Coverage / NEEDS_HUMAN / Advisor UI 엔드포인트 ─────────

    @app.get("/api/v1/nodes/{node_id}/coverage")
    async def api_node_coverage(
        node_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """CoverageReport + atomic_state summary — 사이드패널 상세."""
        try:
            from engine.core.coverage import CoverageVerifier
            from engine.core.state_store import AtomicStateStore
            # engagement_id 찾기 (노드 → 프로젝트 → engagement)
            row = await db.fetchone(
                """SELECT p.engagement_id AS eid FROM nodes n
                   JOIN projects p ON p.id=n.project_id WHERE n.id=?""",
                (node_id,),
            )
            if not row:
                return {"error": "node not found"}
            eid = row["eid"]
            store = AtomicStateStore(db)
            ver = CoverageVerifier(db, store)
            report = await ver.get_report(eid, node_id)
            summary = await store.summary(eid, node_id)
            return {
                "engagement_id": eid,
                "report": None if report is None else {
                    "expected": report.expected_count,
                    "produced": report.produced_count,
                    "missing": report.missing,
                    "ratio": round(report.coverage_ratio, 3),
                    "complete": report.is_complete,
                    "retry_attempts": report.retry_attempts,
                },
                "state_summary": summary,
            }
        except Exception as e:
            return {"error": str(e)[:200]}

    # ── Stage 26: PRD Clarifier 엔드포인트 ─────────────────────────────

    @app.get("/api/v1/engagements/{engagement_id}/clarifications")
    async def api_get_clarifications(
        engagement_id: str,
        only_blocking: bool = False,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        try:
            from engine.intake.prd_clarifier import PRDClarifier
            clar = PRDClarifier(db)
            items = await clar.get_unanswered(engagement_id, only_blocking)
            return {"engagement_id": engagement_id, "items": items, "count": len(items)}
        except Exception as e:
            return {"error": str(e)[:200]}

    @app.post("/api/v1/engagements/{engagement_id}/clarifications/{question_id}/answer")
    async def api_answer_clarification(
        engagement_id: str,
        question_id: str,
        body: dict,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        try:
            from engine.intake.prd_clarifier import PRDClarifier
            clar = PRDClarifier(db)
            await clar.save_answer(engagement_id, question_id, str(body.get("answer", ""))[:2000])
            remaining_blocking = not await clar.all_blocking_answered(engagement_id)
            return {"ok": True, "blocking_remaining": remaining_blocking}
        except Exception as e:
            return {"error": str(e)[:200]}

    @app.get("/api/v1/engagements/{engagement_id}/review")
    async def api_review_needed(
        engagement_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """Stage 17 Human-in-the-loop — 검토 필요 item 목록."""
        try:
            rows = await db.fetchall(
                """SELECT node_id, item_key, status, retry_count, reason, updated_at
                   FROM atomic_state
                   WHERE engagement_id=? AND status='NEEDS_HUMAN'
                   ORDER BY updated_at DESC""",
                (engagement_id,),
            )
            return {
                "engagement_id": engagement_id,
                "items": [dict(r) for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            return {"error": str(e)[:200]}

    # ── Stage 2-C: Cache 지표 엔드포인트 ─────────────────────────────────

    @app.get("/api/v1/metrics/cache")
    async def api_metrics_cache():
        """Prompt Cache 지표 (프로세스 시작 이후 누적).

        - call_count: 총 API 호출 수
        - total_input_tokens_billed: 과금된 input 토큰 합
        - total_cache_read_tokens: 캐시에서 읽어 과금 제외된 토큰
        - cache_hit_ratio: cache_read / (cache_read + billed_input)
        - tokens_saved_by_cache: 캐시 덕에 아낀 토큰
        """
        try:
            from engine.ai.model_adapter import CACHE_METRICS
            return CACHE_METRICS.snapshot()
        except Exception as e:
            return {"error": str(e)[:200]}

    # ── Stage 15: Observability 통합 대시보드 (JSON) ─────────────────────

    @app.get("/api/v1/metrics/dashboard")
    async def api_metrics_dashboard(
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        """모든 주요 지표를 한 번에 반환 — Flow 뷰 대시보드 카드용.

        포함:
        - prompt_cache: CacheMetrics snapshot
        - content_cache: ContentHashCache.stats (DB 집계)
        - advisor_circuits: 활성 circuit breaker 목록
        - token_totals: phase 별 누적 토큰 (기존 agent_token_usage 집계)
        - 529_count_last_hour: 최근 1시간 overload 빈도
        """
        out: dict = {}
        try:
            from engine.ai.model_adapter import CACHE_METRICS
            out["prompt_cache"] = CACHE_METRICS.snapshot()
        except Exception as e:
            out["prompt_cache"] = {"error": str(e)[:120]}

        try:
            from engine.core.content_cache import ContentHashCache
            cache = ContentHashCache(db)
            out["content_cache"] = await cache.stats()
        except Exception as e:
            out["content_cache"] = {"error": str(e)[:120]}

        try:
            # agent_token_usage 집계 (phase 별 최근 24시간)
            rows = await db.fetchall(
                """SELECT phase,
                          COALESCE(SUM(input_tokens), 0) AS input,
                          COALESCE(SUM(output_tokens), 0) AS output,
                          COUNT(*) AS calls
                   FROM agent_token_usage
                   WHERE recorded_at > datetime('now','-1 day')
                   GROUP BY phase"""
            )
            out["token_totals_24h"] = [dict(r) for r in rows]
        except Exception as e:
            out["token_totals_24h"] = {"error": str(e)[:120]}

        return out

    # ── HTMX 단편 (시스템 요약 자동 갱신) ───────────────────────────────

    @app.get("/dashboard/fragments/system-summary", response_class=HTMLResponse)
    async def fragment_system_summary(
        request: Request,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        nodes = await db.fetchone(
            """SELECT
               SUM(CASE WHEN state='IN_PROGRESS' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN state='NEEDS_HUMAN' THEN 1 ELSE 0 END) AS needs_human,
               SUM(CASE WHEN state='SUSPENDED'   THEN 1 ELSE 0 END) AS suspended
               FROM nodes"""
        )
        shutting_down = False
        try:
            from api.server import state as app_state
            shutting_down = app_state.shutdown_manager.is_shutting_down
        except Exception:
            pass

        status = {
            "active_agents": (nodes["active"] or 0) if nodes else 0,
            "needs_human": (nodes["needs_human"] or 0) if nodes else 0,
            "suspended": (nodes["suspended"] or 0) if nodes else 0,
            "shutting_down": shutting_down,
        }

        # 인라인 HTML 단편 반환
        html = f"""
        <div id="system-summary"
             hx-get="/dashboard/fragments/system-summary"
             hx-trigger="every 30s"
             hx-swap="outerHTML">
          <div class="grid grid-4" style="margin-bottom:1rem;">
            <div class="card">
              <div class="card-title">활성 에이전트</div>
              <div class="stat-value" style="color:var(--info)">{status['active_agents']}</div>
              <div class="stat-label">IN_PROGRESS</div>
            </div>
            <div class="card">
              <div class="card-title">인간 개입 필요</div>
              <div class="stat-value" style="color:var(--danger)">{status['needs_human']}</div>
              <div class="stat-label">NEEDS_HUMAN</div>
            </div>
            <div class="card">
              <div class="card-title">일시 중단</div>
              <div class="stat-value" style="color:var(--warning)">{status['suspended']}</div>
              <div class="stat-label">SUSPENDED</div>
            </div>
            <div class="card">
              <div class="card-title">종료 중</div>
              <div class="stat-value">{'예' if status['shutting_down'] else '아니오'}</div>
              <div class="stat-label">Shutdown</div>
            </div>
          </div>
        </div>
        """
        return HTMLResponse(html)

    # ── 시스템 상태 ──────────────────────────────────────────────────────────

    @app.get("/status", response_class=HTMLResponse)
    async def system_status_page(
        request: Request,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        nodes = await db.fetchone(
            """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN state='IN_PROGRESS'       THEN 1 ELSE 0 END) AS in_progress,
               SUM(CASE WHEN state='COMPLETED'         THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN state='NEEDS_HUMAN'       THEN 1 ELSE 0 END) AS needs_human,
               SUM(CASE WHEN state='SUSPENDED'         THEN 1 ELSE 0 END) AS suspended,
               SUM(CASE WHEN state='FAILED'            THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN state='BLOCKED'           THEN 1 ELSE 0 END) AS blocked,
               SUM(CASE WHEN state='AWAITING_APPROVAL' THEN 1 ELSE 0 END) AS awaiting
               FROM nodes"""
        )
        engagements = await db.fetchone(
            """SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) AS active
               FROM engagements"""
        )
        recent_events = await db.fetchall(
            "SELECT event_type, recorded_at, aggregate_type FROM event_store "
            "ORDER BY seq DESC LIMIT 20"
        )
        alerts = await db.fetchall(
            "SELECT id, severity, condition_name, fired_at FROM alert_firings "
            "ORDER BY fired_at DESC LIMIT 10"
        ) if False else []  # alert_firings 테이블 없을 수 있음
        shutting_down = False
        try:
            from api.server import state as app_state
            shutting_down = app_state.shutdown_manager.is_shutting_down
        except Exception:
            pass
        return templates.TemplateResponse(
            "system_status.html",
            {
                "request": request,
                "active_page": "status",
                "nodes": dict(nodes) if nodes else {},
                "engagements": dict(engagements) if engagements else {},
                "recent_events": [dict(r) for r in recent_events],
                "shutting_down": shutting_down,
            },
        )

    # ── 스킬 관리 ──────────────────────────────────────────────────────────

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request):
        from engine.skills.registry import SkillRegistry
        registry = SkillRegistry()
        all_skills = registry.list_all()
        phases = set(s.get("phase", "").lower() for s in all_skills)
        return templates.TemplateResponse(
            "skills.html",
            {
                "request": request,
                "active_page": "skills",
                "total_skills": len(all_skills),
                "total_phases": len(phases),
            },
        )

    # ── 크리덴셜 관리 ──────────────────────────────────────────────────────

    @app.get("/credentials", response_class=HTMLResponse)
    async def credentials_page(request: Request):
        return templates.TemplateResponse(
            "credentials.html",
            {"request": request, "active_page": "credentials"},
        )

    # ── 공개 인테이크 폼 (로그인 불필요) ─────────────────────────────────────

    @app.get("/intake/new", response_class=HTMLResponse)
    async def intake_public_page(request: Request):
        from fastapi.responses import FileResponse
        import os as _os
        tpl = _os.path.join(_os.path.dirname(__file__), "templates", "intake_public.html")
        return FileResponse(tpl)

    # ── 로그인 ──────────────────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        from fastapi.responses import FileResponse
        import os as _os
        tpl_path = _os.path.join(_os.path.dirname(__file__), "templates", "login.html")
        return FileResponse(tpl_path)

    # ── 인테이크 폼 ────────────────────────────────────────────────────────

    @app.get("/intake", response_class=HTMLResponse)
    async def intake_form_page(request: Request):
        return templates.TemplateResponse(
            "intake_form.html",
            {"request": request, "active_page": "intake"},
        )

    @app.get("/intake/submissions", response_class=HTMLResponse)
    async def intake_submissions_page(request: Request):
        return templates.TemplateResponse(
            "intake_submissions.html",
            {"request": request, "active_page": "intake_sub"},
        )

    # ── 디자인 프리뷰 ─────────────────────────────────────────────────

    @app.get("/engagements/{engagement_id}/design-preview", response_class=HTMLResponse)
    async def design_preview_page(
        request: Request,
        engagement_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        engagement = await db.fetchone(
            "SELECT id, name, client_name, status FROM engagements WHERE id=?",
            (engagement_id,),
        )
        if not engagement:
            raise HTTPException(status_code=404, detail="인게이지먼트 없음")

        # DESIGN 단계 HTML 산출물 조회 (시안, 화면설계서, IA 등)
        design_artifacts = await db.fetchall(
            """SELECT n.id, n.name, n.state, n.phase,
                      a.id as artifact_id, a.artifact_type, a.current_version,
                      p.id as project_id
               FROM nodes n
               JOIN dags d ON d.id = n.dag_id
               JOIN projects p ON p.id = d.project_id
               LEFT JOIN artifacts a ON a.node_id = n.id
               WHERE p.engagement_id = ? AND n.node_type = 'TASK'
                 AND n.state = 'COMPLETED' AND a.id IS NOT NULL
                 AND (n.phase = 'DESIGN' AND a.artifact_type = 'html')
               ORDER BY n.priority, n.created_at""",
            (engagement_id,),
        )

        return templates.TemplateResponse(
            "design_preview.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagement": dict(engagement),
                "design_artifacts": [dict(r) for r in design_artifacts],
            },
        )

    # ── 산출물 전체 보기 ─────────────────────────────────────────────────

    @app.get("/engagements/{engagement_id}/deliverables", response_class=HTMLResponse)
    async def deliverables_page(
        request: Request,
        engagement_id: str,
        db: "DatabaseAdapter" = Depends(get_db_func),
    ):
        engagement = await db.fetchone(
            "SELECT id, name, client_name, status FROM engagements WHERE id=?",
            (engagement_id,),
        )
        if not engagement:
            raise HTTPException(status_code=404, detail="인게이지먼트 없음")

        # 프로젝트 목록
        projects = await db.fetchall(
            "SELECT id, name FROM projects WHERE engagement_id=? ORDER BY created_at",
            (engagement_id,),
        )

        # 완료된 TASK 노드 + 산출물 정보 조회 (단일 JOIN — N+1 제거)
        project_ids = [p["id"] for p in projects]
        project_map = {p["id"]: p["name"] for p in projects}
        all_nodes = []
        if project_ids:
            placeholders = ",".join("?" for _ in project_ids)
            all_nodes = await db.fetchall(
                f"""SELECT n.id, n.name, n.phase, n.state, n.node_type,
                           a.id as artifact_id, a.artifact_type, a.current_version,
                           d.project_id
                    FROM nodes n
                    JOIN dags d ON d.id = n.dag_id
                    LEFT JOIN artifacts a ON a.node_id = n.id
                    WHERE d.project_id IN ({placeholders}) AND n.node_type = 'TASK' AND n.state != 'SKIPPED'
                    ORDER BY d.project_id,
                      CASE n.phase
                        WHEN 'DEFINE' THEN 1 WHEN 'DESIGN' THEN 2
                        WHEN 'BUILD' THEN 3 WHEN 'VERIFY' THEN 4
                        WHEN 'DELIVER' THEN 5 ELSE 9
                      END,
                      n.priority, n.created_at""",
                tuple(project_ids),
            )

        artifacts_data = []
        # 프로젝트별 그룹핑
        from collections import defaultdict
        nodes_by_project = defaultdict(list)
        for n in all_nodes:
            nodes_by_project[n["project_id"]].append(n)

        for proj_id in project_ids:
            proj_artifacts = {
                "project_id": proj_id,
                "project_name": project_map[proj_id],
                "phases": {},
            }
            for n in nodes_by_project.get(proj_id, []):
                phase = n["phase"]
                if phase not in proj_artifacts["phases"]:
                    proj_artifacts["phases"][phase] = {
                        "label": PHASE_LABEL.get(phase, phase),
                        "nodes": [],
                    }
                proj_artifacts["phases"][phase]["nodes"].append(dict(n))
            # 순서 보장
            proj_artifacts["phase_list"] = [
                {"name": ph, "label": PHASE_LABEL.get(ph, ph), "nodes": proj_artifacts["phases"][ph]["nodes"]}
                for ph in PHASE_ORDER
                if ph in proj_artifacts["phases"]
            ]
            artifacts_data.append(proj_artifacts)

        # 전체 통계
        total_tasks = sum(
            len(item)
            for pd in artifacts_data
            for ph in pd.get("phase_list", [])
            for item in [ph["nodes"]]
        )
        completed_tasks = sum(
            1
            for pd in artifacts_data
            for ph in pd.get("phase_list", [])
            for item in ph["nodes"]
            if item["state"] == "COMPLETED"
        )
        with_artifact = sum(
            1
            for pd in artifacts_data
            for ph in pd.get("phase_list", [])
            for item in ph["nodes"]
            if item.get("artifact_id")
        )

        # 프리뷰 URL 감지 — workspace 디렉토리에 서버가 구동 중인지 확인
        import asyncio as _aio, json as _json
        preview_urls = []
        api_docs_url = None
        _ws_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspaces")
        _eng_name = engagement["name"]
        # engagement name → workspace 디렉토리 매칭 (정확 → 접두어 → 포함)
        workspace_dir = os.path.join(_ws_root, _eng_name)
        if not os.path.isdir(workspace_dir):
            _short = _eng_name.split(" — ")[0].split(" - ")[0].strip()
            workspace_dir = os.path.join(_ws_root, _short)
        if not os.path.isdir(workspace_dir) and os.path.isdir(_ws_root):
            for _d in os.listdir(_ws_root):
                if _d.lower() == _short.lower() or _short.lower() in _d.lower():
                    workspace_dir = os.path.join(_ws_root, _d)
                    break
        if os.path.isdir(workspace_dir):
            ports_file = os.path.join(workspace_dir, "ports.json")
            if os.path.isfile(ports_file):
                with open(ports_file) as _f:
                    _ports = _json.load(_f)
                frontend_port = _ports.get("frontend", 3000)
                backend_port = _ports.get("backend", 4000)
                api_docs_path = _ports.get("api_docs_path", "/docs")
            else:
                frontend_port = 3000
                backend_port = 4000
                api_docs_path = "/docs"

            async def _check_port(port: int) -> bool:
                try:
                    _, w = await _aio.wait_for(
                        _aio.open_connection("127.0.0.1", port), timeout=0.5,
                    )
                    w.close()
                    await w.wait_closed()
                    return True
                except (OSError, _aio.TimeoutError):
                    return False

            fe_up, be_up = await _aio.gather(
                _check_port(frontend_port), _check_port(backend_port),
            )
            if fe_up:
                preview_urls.append({"label": "프론트엔드", "url": f"http://localhost:{frontend_port}"})
            if be_up:
                preview_urls.append({"label": "백엔드 API", "url": f"http://localhost:{backend_port}"})
                api_docs_url = f"http://localhost:{backend_port}{api_docs_path}"

        return templates.TemplateResponse(
            "deliverables.html",
            {
                "request": request,
                "active_page": "engagements",
                "engagement": dict(engagement),
                "artifacts_data": artifacts_data,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "with_artifact": with_artifact,
                "preview_urls": preview_urls,
                "api_docs_url": api_docs_url,
            },
        )
