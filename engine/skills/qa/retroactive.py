"""
engine/skills/qa/retroactive.py
소급 검증(Retroactive Validation) — 새 harness 규칙을 기존 COMPLETED 산출물에 적용.

동작 원리:
  1. COMPLETED 상태의 DESIGN/BUILD 산출물을 로드
  2. 현재 harness 규칙으로 재검증
  3. FAIL 시 관련 TASK→INVALID, QA→NOT_STARTED 로 전환
  4. DAGAdvancer가 INVALID 노드를 자동으로 재실행

안전성:
  - 읽기 위주 + 상태 전환만 (COMPLETED→INVALID, QA→NOT_STARTED)
  - 멱등성 보장 (이미 INVALID인 노드는 건드리지 않음)
  - DB 락 에러 시 graceful skip
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────────────────────────────────────────────────────
# Main: run_retroactive_validation
# ───────────────────────────────────────────────────────────────────────────

async def run_retroactive_validation(
    db,
    project_id: str | None = None,
) -> list[dict]:
    """기존 COMPLETED 산출물을 최신 harness 규칙으로 소급 검증.

    Args:
        db: DatabaseAdapter 인스턴스
        project_id: 특정 프로젝트만 검증 (None이면 전체)

    Returns:
        검증 결과 리스트 [{"project_id", "check_name", "result", "failures", ...}, ...]
    """
    results: list[dict] = []

    # ── retroactive_validations 테이블 보장 ──
    await _ensure_table(db)

    # ── 엔진 코드 버전 변경 감지 → 영향 노드 자동 무효화 ──
    try:
        from engine.lifecycle.engine_version import check_and_invalidate_on_engine_change
        engine_results = await check_and_invalidate_on_engine_change(db)
        if engine_results:
            logger.info(
                "engine_version_invalidation count=%d groups=%s",
                len(engine_results),
                list({r["group"] for r in engine_results}),
            )
    except Exception as exc:
        logger.warning("engine_version_check_failed error=%s", str(exc))

    # ── 대상 프로젝트 로드 ──
    if project_id:
        projects = await db.fetchall(
            "SELECT id, name FROM projects WHERE id=?", (project_id,)
        )
    else:
        projects = await db.fetchall("SELECT id, name FROM projects")

    for proj in projects:
        pid = proj["id"]
        pname = proj["name"]
        try:
            # DESIGN 단계 검증: 화면 커버리지
            design_results = await _validate_design_coverage(db, pid, pname)
            results.extend(design_results)

            # BUILD 단계 검증: 코드 품질 + 인터랙티비티
            build_results = await _validate_build_artifacts(db, pid, pname)
            results.extend(build_results)
        except Exception as exc:
            logger.warning(
                "retroactive_validation_error project=%s error=%s",
                pid[:8], str(exc),
            )
            results.append({
                "project_id": pid,
                "check_name": "error",
                "result": "ERROR",
                "failures": [str(exc)],
            })

    logger.info(
        "retroactive_validation_complete projects=%d results=%d",
        len(projects), len(results),
    )

    # post-event hook — wave-engine A4 retroactive 가 DEFINE phase 검증 추가
    try:
        from engine.core.hook_registry import call_hooks
        hook_results = await call_hooks(
            "post_retroactive_validation", db, project_id, results,
        )
        for hr in hook_results:
            if isinstance(hr, list):
                results.extend(hr)
    except Exception:
        pass

    return results


# ───────────────────────────────────────────────────────────────────────────
# DESIGN 단계: 화면 커버리지 소급 검증
# ───────────────────────────────────────────────────────────────────────────

async def _validate_design_coverage(db, project_id: str, project_name: str) -> list[dict]:
    """화면 목록 정의서 vs 디자인 시안/레시피 커버리지 재검증."""
    results: list[dict] = []

    # UI 디자인 시안 TASK 노드 (COMPLETED 상태만)
    design_task = await db.fetchone(
        "SELECT id, state FROM nodes WHERE project_id=? AND name='UI 디자인 시안' AND node_type='TASK'",
        (project_id,),
    )
    # 페이지 레시피 TASK 노드 (COMPLETED 상태만)
    recipe_task = await db.fetchone(
        "SELECT id, state FROM nodes WHERE project_id=? AND name='페이지 레시피' AND node_type='TASK'",
        (project_id,),
    )

    # 둘 다 COMPLETED가 아니면 검증 불필요
    if not design_task or design_task["state"] != "COMPLETED":
        return results
    if not recipe_task or recipe_task["state"] != "COMPLETED":
        return results

    # 화면 목록 정의서 내용 로드
    screen_list_row = await db.fetchone(
        """SELECT av.storage_path FROM artifacts a
           JOIN artifact_versions av ON a.id=av.artifact_id
           WHERE a.project_id=? AND a.node_id IN
             (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
           AND av.version_num = a.current_version""",
        (project_id, project_id),
    )
    if not screen_list_row or not screen_list_row["storage_path"]:
        return results

    screen_content = screen_list_row["storage_path"]

    # 레시피 slugs — composition_recipes 우선, fallback 으로 artifact JSON spec 의 scr_id 직접 추출 (B6).
    recipe_rows = await db.fetchall(
        "SELECT page_slug FROM composition_recipes WHERE project_id=?",
        (project_id,),
    )
    recipe_slugs = [r["page_slug"] for r in recipe_rows]
    if not recipe_slugs:
        # composition_recipes 미생성 (DESIGN 단계 직후) → 페이지 레시피 artifact 의 JSON spec 직접 분석.
        recipe_art_row = await db.fetchone(
            """SELECT av.storage_path FROM artifacts a
               JOIN artifact_versions av ON a.id=av.artifact_id
               WHERE a.node_id=? AND av.version_num=a.current_version""",
            (recipe_task["id"],),
        )
        if recipe_art_row and recipe_art_row.get("storage_path"):
            import re as _re_recipe
            recipe_content = recipe_art_row["storage_path"]
            scr_ids = _re_recipe.findall(
                r'"scr_id"\s*:\s*"(SC-[A-Z]{2,4}-\d{3,4})"',
                recipe_content,
            )
            if scr_ids:
                recipe_slugs = [f"scr-{rid.lower()}" for rid in scr_ids]

    # 디자인 시안 slugs — workspace preview 폴더 우선 (BUILD 단계 산출),
    # 폴더 부재 시(DESIGN 단계 직후) DB artifact 의 section ID 직접 추출 (B5).
    from engine.workspace.paths import WORKSPACES_ROOT, _make_slug
    ws_path = WORKSPACES_ROOT / _make_slug(project_name) / "preview"
    design_slugs: list[str] = []
    if ws_path.is_dir():
        design_slugs = [
            f.stem for f in ws_path.iterdir()
            if f.suffix == ".html" and f.stem != "index"
        ]
    if not design_slugs:
        design_art_row = await db.fetchone(
            """SELECT av.storage_path FROM artifacts a
               JOIN artifact_versions av ON a.id=av.artifact_id
               WHERE a.node_id=? AND av.version_num=a.current_version""",
            (design_task["id"],),
        )
        if design_art_row and design_art_row.get("storage_path"):
            import re as _re_design
            design_html = design_art_row["storage_path"]
            section_ids = set(_re_design.findall(
                r'<section[^>]*\bid=["\'](SC-[A-Z]{2,5}-\d{3,4})["\']',
                design_html,
            ))
            design_slugs = [f"scr-{sid.lower()}" for sid in sorted(section_ids)]

    # harness 검증 실행
    from engine.skills.qa.harness import _harness_validate_screen_coverage
    cov_result = _harness_validate_screen_coverage(
        screen_content, design_slugs, recipe_slugs,
    )

    if cov_result["pass"]:
        results.append({
            "project_id": project_id,
            "check_name": "screen_coverage",
            "result": "PASS",
            "failures": [],
        })
        logger.info(
            "retroactive_screen_coverage_pass project=%s",
            project_id[:8],
        )
        return results

    # ── FAIL → 노드 무효화 ──
    failures = cov_result.get("structural_failures", [])
    invalidated_nodes: list[str] = []

    # 무효화 대상 노드 목록
    invalidation_targets = [
        # (name, node_type, new_state)
        ("UI 디자인 시안", "TASK", "INVALID"),
        ("페이지 레시피", "TASK", "INVALID"),
        ("[QA] UI 디자인 시안", "QA", "NOT_STARTED"),
        ("[QA] 페이지 레시피", "QA", "NOT_STARTED"),
    ]

    for node_name, node_type, new_state in invalidation_targets:
        node = await db.fetchone(
            "SELECT id, state FROM nodes WHERE project_id=? AND name=? AND node_type=?",
            (project_id, node_name, node_type),
        )
        if node and node["state"] == "COMPLETED":
            try:
                await db.execute(
                    "UPDATE nodes SET state=?, updated_at=?, version=version+1 WHERE id=?",
                    (new_state, _now(), node["id"]),
                )
                invalidated_nodes.append(node["id"])
                logger.info(
                    "retroactive_invalidated node=%s name='%s' new_state=%s project=%s",
                    node["id"][:8], node_name, new_state, project_id[:8],
                )
            except Exception as exc:
                logger.warning(
                    "retroactive_invalidate_failed node=%s error=%s",
                    node["id"][:8], str(exc),
                )

    # DESIGN→BUILD GATE 리셋
    gate = await db.fetchone(
        """SELECT id, state FROM nodes
           WHERE project_id=? AND node_type='GATE' AND phase='DESIGN'
             AND state='COMPLETED'""",
        (project_id,),
    )
    if gate:
        try:
            await db.execute(
                "UPDATE nodes SET state='NOT_STARTED', updated_at=?, version=version+1 WHERE id=?",
                (_now(), gate["id"]),
            )
            invalidated_nodes.append(gate["id"])
            logger.info(
                "retroactive_gate_reset gate=%s project=%s",
                gate["id"][:8], project_id[:8],
            )
        except Exception as exc:
            logger.warning("retroactive_gate_reset_failed error=%s", str(exc))

    # 검증 기록 저장
    record = {
        "project_id": project_id,
        "check_name": "screen_coverage",
        "result": "FAIL",
        "failures": failures,
        "invalidated_nodes": invalidated_nodes,
    }
    await _store_record(db, record)
    results.append(record)

    logger.warning(
        "retroactive_screen_coverage_fail project=%s failures=%d invalidated=%d",
        project_id[:8], len(failures), len(invalidated_nodes),
    )
    return results


# ───────────────────────────────────────────────────────────────────────────
# BUILD 단계: 코드 품질 + 인터랙티비티 소급 검증
# ───────────────────────────────────────────────────────────────────────────

async def _validate_build_artifacts(db, project_id: str, project_name: str) -> list[dict]:
    """BUILD 단계 COMPLETED 코드 산출물을 재검증."""
    results: list[dict] = []

    # 프론트엔드 컴포넌트 구현 TASK 노드들 (COMPLETED)
    build_nodes = await db.fetchall(
        """SELECT id, name, state FROM nodes
           WHERE project_id=? AND phase='BUILD'
             AND node_type='TASK' AND state='COMPLETED'
             AND name LIKE '%프론트엔드 컴포넌트 구현%'""",
        (project_id,),
    )

    for node in build_nodes:
        node_id = node["id"]
        node_name = node["name"]

        # 최신 산출물 내용 로드
        art_row = await db.fetchone(
            """SELECT av.storage_path FROM artifacts a
               JOIN artifact_versions av ON a.id=av.artifact_id
               WHERE a.node_id=? AND av.version_num = a.current_version""",
            (node_id,),
        )
        if not art_row or not art_row["storage_path"]:
            continue

        content = art_row["storage_path"]

        # ── harness 검증: AI 코드 구조 ──
        from engine.skills.qa.harness import _harness_validate_ai_code
        code_result = _harness_validate_ai_code(content, node_name, None)

        # ── harness 검증: 인터랙티비티 ──
        from engine.skills.qa.harness import _harness_validate_interactivity
        interactivity_result = _harness_validate_interactivity(content, node_name, None)

        all_pass = code_result["pass"] and interactivity_result["pass"]
        all_failures = (
            code_result.get("structural_failures", [])
            + interactivity_result.get("structural_failures", [])
        )

        if all_pass:
            results.append({
                "project_id": project_id,
                "node_id": node_id,
                "check_name": "build_code_quality",
                "result": "PASS",
                "failures": [],
            })
            continue

        # ── FAIL → TASK INVALID + QA NOT_STARTED ──
        invalidated_nodes: list[str] = []
        try:
            await db.execute(
                "UPDATE nodes SET state='NOT_STARTED', retry_count=0, updated_at=?, version=version+1 WHERE id=?",
                (_now(), node_id),
            )
            invalidated_nodes.append(node_id)
            logger.info(
                "retroactive_build_invalidated node=%s name='%s' project=%s",
                node_id[:8], node_name, project_id[:8],
            )
        except Exception as exc:
            logger.warning(
                "retroactive_build_invalidate_failed node=%s error=%s",
                node_id[:8], str(exc),
            )

        # QA 쌍 노드 리셋
        qa_node = await db.fetchone(
            "SELECT id, state FROM nodes WHERE task_pair_node_id=? AND node_type='QA'",
            (node_id,),
        )
        if qa_node and qa_node["state"] == "COMPLETED":
            try:
                await db.execute(
                    "UPDATE nodes SET state='NOT_STARTED', updated_at=?, version=version+1 WHERE id=?",
                    (_now(), qa_node["id"]),
                )
                invalidated_nodes.append(qa_node["id"])
            except Exception as exc:
                logger.warning(
                    "retroactive_qa_reset_failed node=%s error=%s",
                    qa_node["id"][:8], str(exc),
                )

        record = {
            "project_id": project_id,
            "node_id": node_id,
            "check_name": "build_code_quality",
            "result": "FAIL",
            "failures": all_failures,
            "invalidated_nodes": invalidated_nodes,
        }
        await _store_record(db, record)
        results.append(record)

        logger.warning(
            "retroactive_build_fail node=%s failures=%d project=%s",
            node_id[:8], len(all_failures), project_id[:8],
        )

    return results


# ───────────────────────────────────────────────────────────────────────────
# 스케줄: 24시간 주기 자동 실행
# ───────────────────────────────────────────────────────────────────────────

async def schedule_retroactive_check(db) -> None:
    """서버 시작 시 1회 호출. 24시간 이상 경과 시 전체 소급 검증 실행."""
    try:
        await _ensure_table(db)

        # retroactive_last_run 플래그 확인
        row = await db.fetchone(
            "SELECT value FROM kv_store WHERE key='retroactive_last_run'"
        )
        last_run: str | None = row["value"] if row else None

        should_run = True
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                elapsed = datetime.now(timezone.utc) - last_dt
                if elapsed.total_seconds() < 86400:  # 24h
                    should_run = False
                    logger.info(
                        "retroactive_check_skipped last_run=%s hours_ago=%.1f",
                        last_run, elapsed.total_seconds() / 3600,
                    )
            except (ValueError, TypeError):
                pass  # 파싱 실패 → 실행

        if not should_run:
            return

        logger.info("retroactive_check_starting scope=all_projects")
        results = await run_retroactive_validation(db)

        fail_count = sum(1 for r in results if r.get("result") == "FAIL")
        logger.info(
            "retroactive_check_done total=%d fails=%d",
            len(results), fail_count,
        )

        # 타임스탬프 갱신
        now = _now()
        try:
            await db.execute(
                "INSERT INTO kv_store (key, value) VALUES ('retroactive_last_run', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now,),
            )
        except Exception:
            # kv_store 테이블이 없을 수 있음 — 무시
            logger.debug("retroactive_timestamp_save_failed (kv_store may not exist)")

    except Exception as exc:
        logger.warning("retroactive_schedule_error error=%s", str(exc))


# ───────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ───────────────────────────────────────────────────────────────────────────

async def _ensure_table(db) -> None:
    """retroactive_validations 테이블이 없으면 생성."""
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS retroactive_validations (
                id              TEXT PRIMARY KEY,
                project_id      TEXT NOT NULL,
                node_id         TEXT NOT NULL,
                check_name      TEXT NOT NULL,
                result          TEXT NOT NULL,
                failures        TEXT,
                invalidated_nodes TEXT,
                created_at      TEXT NOT NULL
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_retro_project ON retroactive_validations(project_id)"
        )
    except Exception:
        pass  # 이미 존재하면 무시


async def _store_record(db, record: dict) -> None:
    """검증 결과를 retroactive_validations 테이블에 저장."""
    try:
        await db.execute(
            """INSERT INTO retroactive_validations
               (id, project_id, node_id, check_name, result, failures, invalidated_nodes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                record["project_id"],
                record.get("node_id", ""),
                record["check_name"],
                record["result"],
                json.dumps(record.get("failures", []), ensure_ascii=False),
                json.dumps(record.get("invalidated_nodes", []), ensure_ascii=False),
                _now(),
            ),
        )
    except Exception as exc:
        logger.warning("retroactive_store_failed error=%s", str(exc))
