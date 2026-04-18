"""
engine/lifecycle/startup.py
Startup Recovery — 재시작 후 고아 노드 정리.
IN_PROGRESS 고아 노드 → NOT_STARTED (retry<3) 또는 FAILED (retry>=3)
SHUTDOWN_DRAIN 노드 → READY (정상 재개)
SQLite 백업 예약 (매일 03:00).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from engine.db.adapter import DatabaseAdapter
from engine.observability.logger import get_logger

logger = get_logger(__name__)


class StartupRecovery:
    """
    앱 시작 시 run() 한 번 호출.
    1. 좀비 IN_PROGRESS 노드 → SUSPENDED
    2. SHUTDOWN_DRAIN SUSPENDED → READY
    3. Cascade Invalidation pending 배치 처리
    """

    # 좀비 감지 임계값: 5분 이상 heartbeat 없음
    ZOMBIE_THRESHOLD_MINUTES = 5

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    async def run(self) -> dict:
        """
        전체 복구 절차 실행.
        반환: 처리 요약.
        """
        result = {
            "zombies_suspended": 0,
            "shutdown_drain_resumed": 0,
            "transient_suspended_resumed": 0,
            "cascade_pending_processed": 0,
            "garbage_nodes_deleted": 0,
            "pair_links_healed": 0,
            "qa_unblocked": 0,
        }

        # 순서 주의:
        # 1) 잘못된 분할 노드 SKIPPED 처리 (garbage)
        # 2) 동일 이름 중복 노드 dedup (SKIPPED 로 축소)
        #    → 여기까지 끝내야 헬링 대상이 "살아있는 유일 노드" 로 수렴.
        # 3) QA edge 보정
        # 4) pair link self-heal (SKIPPED 제외 상태에서 매칭하므로 오매칭 방지)
        result["garbage_nodes_deleted"] = await self._cleanup_garbage_nodes()
        result["garbage_nodes_deleted"] += await self._dedup_split_nodes()
        await self._repair_qa_edges()
        result["pair_links_healed"] = await self._heal_pair_links()
        # 해결된/무효 gotchas 정리
        try:
            cleaned = await self._db.execute(
                """DELETE FROM project_gotchas
                   WHERE source_node_id IN (
                       SELECT id FROM nodes WHERE state IN ('COMPLETED', 'SKIPPED')
                   )"""
            )
            if cleaned:
                logger.info("startup_gotchas_cleaned count=%d", cleaned)
        except Exception:
            pass
        # SKIPPED 노드의 잔여 토큰 기록 정리 (분할로 SKIPPED된 원본 등)
        try:
            _token_cleaned = await self._db.execute(
                """DELETE FROM agent_token_usage
                   WHERE node_id IN (
                       SELECT id FROM nodes WHERE state = 'SKIPPED'
                   )"""
            )
            if _token_cleaned:
                logger.info("startup_skipped_token_cleaned count=%d", _token_cleaned)
        except Exception:
            pass
        # 서버 재시작 시 IN_PROGRESS 좀비 즉시 정리 (executor 프로세스가 없으므로 안전)
        result["zombies_suspended"] = await self._handle_zombie_nodes()
        result["shutdown_drain_resumed"] = await self._resume_shutdown_drain()
        # 일시 오류(API 529/429 등)로 SUSPENDED된 노드를 재시작 시 1회 즉시 복원
        result["transient_suspended_resumed"] = await self._resume_transient_suspended()
        # TASK 재개 후에도 BLOCKED 상태로 남아있는 QA 짝 노드를 NOT_STARTED로 리셋
        result["qa_unblocked"] = await self._unblock_stale_qa()
        result["cascade_pending_processed"] = await self._process_cascade_pending()
        # 실패한 노드 중 description이 비어있는 경우 project_gotchas에서 사유 sync
        result["failure_reasons_synced"] = await self._sync_failure_reasons()
        # harness 버그 수정 후 같은 원인으로 SUSPENDED된 노드 재평가 → 새 규칙에서
        # 통과하면 자동 복원. 직접 DB 조작 대신 엔진이 자체 처리하는 범용 경로.
        result["harness_regressions_recovered"] = await self._recover_harness_regression_suspended()

        # DAG 정합성 검증 + 자동 복구 (startup hook)
        # 158+80건 같은 누적 방지 — 매 startup 마다 정기 검증.
        # 위험 항목 (cycle/pair_inconsistency) 은 로그만, 안전 항목 (SKIPPED edge /
        # orphan edge) 은 자동 복구.
        try:
            from engine.tools.verify_dag_integrity import run_integrity_check
            integrity = await run_integrity_check(self._db, apply=True)
            result["integrity_issues"] = integrity.get("counts", {})
            result["integrity_fixed"] = integrity.get("fixed") or {}
            if result["integrity_issues"]:
                logger.info(
                    "startup_dag_integrity issues=%s fixed=%s",
                    result["integrity_issues"], result["integrity_fixed"],
                )
        except Exception as _ie:
            logger.warning("startup_dag_integrity_failed err=%s", _ie)

        logger.info(
            "startup_recovery_complete garbage_deleted=%s pair_links_healed=%s "
            "zombies_suspended=%s shutdown_drain_resumed=%s transient_resumed=%s "
            "qa_unblocked=%s cascade_pending_processed=%s",
            result["garbage_nodes_deleted"], result["pair_links_healed"],
            result["zombies_suspended"], result["shutdown_drain_resumed"],
            result["transient_suspended_resumed"], result["qa_unblocked"],
            result["cascade_pending_processed"],
        )
        return result

    # ------------------------------------------------------------------
    # Pair link self-heal (QA ↔ TASK 양방향 링크 복원)
    # ------------------------------------------------------------------

    async def _heal_pair_links(self) -> int:
        """QA/TASK pair link 누락을 이름 매칭으로 범용 복원.

        대상:
        1. TASK에 qa_pair_node_id NULL 이고, 동일 project/dag에 '[QA] <name>' 노드 존재
        2. QA에 task_pair_node_id NULL 이고, 동일 project/dag에 '<name>' (prefix 제거) TASK 존재

        이름 prefix는 '[QA] ' 하드코딩 (엔진 표준). 프로젝트별 맞춤 없음.
        반환: 복원된 링크 개수 (양방향 update 1건을 1로 카운트).
        """
        now = _now()
        healed = 0

        # ── TASK → QA 방향 ──
        task_rows = await self._db.fetchall(
            """SELECT id, dag_id, project_id, name FROM nodes
               WHERE node_type='TASK' AND qa_pair_node_id IS NULL
                 AND state NOT IN ('SKIPPED')
               LIMIT 1000"""
        )
        for t in task_rows:
            # 동일 이름 QA 여러 개일 가능성에 대비 — artifact 多 / 최근 것 우선.
            qa_row = await self._db.fetchone(
                """SELECT n.id FROM nodes n
                   WHERE n.dag_id=? AND n.project_id=? AND n.node_type='QA'
                     AND n.name=? AND n.state != 'SKIPPED'
                   ORDER BY
                     (SELECT COUNT(*) FROM artifacts WHERE node_id=n.id) DESC,
                     n.created_at DESC
                   LIMIT 1""",
                (t["dag_id"], t["project_id"], f"[QA] {t['name']}"),
            )
            if not qa_row:
                continue
            await self._db.execute(
                "UPDATE nodes SET qa_pair_node_id=?, updated_at=? WHERE id=? AND qa_pair_node_id IS NULL",
                (qa_row["id"], now, t["id"]),
            )
            await self._db.execute(
                "UPDATE nodes SET task_pair_node_id=?, updated_at=? WHERE id=? AND task_pair_node_id IS NULL",
                (t["id"], now, qa_row["id"]),
            )
            healed += 1

        # ── QA → TASK 방향 (위에서 TASK 쪽이 이미 qa_pair를 갖고 있어 매칭 안 된 경우) ──
        qa_rows = await self._db.fetchall(
            """SELECT id, dag_id, project_id, name FROM nodes
               WHERE node_type='QA' AND task_pair_node_id IS NULL
                 AND state NOT IN ('SKIPPED')
                 AND name LIKE '[QA] %'
               LIMIT 1000"""
        )
        for q in qa_rows:
            _task_name = q["name"][4:] if q["name"].startswith("[QA] ") else q["name"]
            t_row = await self._db.fetchone(
                """SELECT n.id FROM nodes n
                   WHERE n.dag_id=? AND n.project_id=? AND n.node_type='TASK'
                     AND n.name=? AND n.state != 'SKIPPED'
                   ORDER BY
                     (SELECT COUNT(*) FROM artifacts WHERE node_id=n.id) DESC,
                     n.created_at DESC
                   LIMIT 1""",
                (q["dag_id"], q["project_id"], _task_name),
            )
            if not t_row:
                continue
            await self._db.execute(
                "UPDATE nodes SET task_pair_node_id=?, updated_at=? WHERE id=? AND task_pair_node_id IS NULL",
                (t_row["id"], now, q["id"]),
            )
            # 역방향도 비어있으면 동시 채움 (멱등)
            await self._db.execute(
                "UPDATE nodes SET qa_pair_node_id=?, updated_at=? WHERE id=? AND qa_pair_node_id IS NULL",
                (q["id"], now, t_row["id"]),
            )
            healed += 1

        if healed:
            logger.info("startup_pair_links_healed count=%d", healed)
        return healed

    # ------------------------------------------------------------------
    # Transient SUSPENDED 재개 (재시작 시 1회, 이후는 watchdog 담당)
    # ------------------------------------------------------------------

    async def _resume_transient_suspended(self) -> int:
        """일시 오류(API 529/429/overloaded/timeout) SUSPENDED 노드를 NOT_STARTED로 복원.

        범용 로직 — suspension_reason 문자열 패턴 기반. 프로젝트/노드 무관.
        watchdog과 동일한 판정 로직을 공유 (import로 일원화).
        """
        from engine.lifecycle.watchdog import _is_transient_suspension  # 판정 일원화

        rows = await self._db.fetchall(
            """SELECT id, suspension_reason, failure_reasons FROM nodes
               WHERE state='SUSPENDED' LIMIT 1000"""
        )
        ids = [
            r["id"] for r in rows
            if _is_transient_suspension(r["suspension_reason"], r["failure_reasons"])
            and len(r["failure_reasons"] or "") <= 6000  # runaway 방지 (watchdog과 동일 기준)
        ]
        if not ids:
            return 0
        now = _now()
        ph = ",".join(["?"] * len(ids))
        # retry_count 보존 — executor 근본원인 분석이 이를 조건으로 사용.
        await self._db.execute(
            f"""UPDATE nodes
                SET state='NOT_STARTED',
                    suspension_reason=NULL,
                    stall_count=0,
                    updated_at=?, version=version+1
                WHERE id IN ({ph}) AND state='SUSPENDED'""",
            [now] + ids,
        )
        logger.info("startup_transient_suspended_resumed count=%d", len(ids))
        return len(ids)

    # ------------------------------------------------------------------
    # Harness 버그 수정 후 SUSPENDED 노드 자동 재평가
    # ------------------------------------------------------------------

    async def _recover_harness_regression_suspended(self) -> int:
        """SUSPENDED 노드 범용 자동 재평가·복원.

        대상 사유 (description/failure_reasons 에 다음 키워드 포함):
          - harness 계열: harness_document, harness_structural, harness_json,
            harness_interactivity, no_todo, 금지어
          - 방어 발동: stall_limit (반복 실패), token_limit (토큰 누적 상한)

        조건:
          - TASK 노드에 유효한 artifact_version 존재
          - 해당 content를 현재 harness 로직으로 재평가 → structural_failures 없음

        복원:
          - TASK → COMPLETED, stall_count=0
          - paired QA → NOT_STARTED, retry_count=0, stall_count=0
          - token_limit 사유면 agent_token_usage DELETE (재시도 시 다시 쌓일
            수 있도록 카운터 초기화)

        범용성: 특정 노드·프로젝트 이름에 의존하지 않음. 엔진이 자체 판단.
        """
        try:
            rows = await self._db.fetchall(
                """SELECT n.id, n.name, n.node_type, n.task_pair_node_id,
                          n.qa_pair_node_id, n.description
                   FROM nodes n
                   WHERE n.state = 'SUSPENDED' AND n.node_type = 'TASK'
                   LIMIT 200"""
            )
        except Exception as exc:
            logger.warning("harness_regression_query_failed: %s", exc)
            return 0

        if not rows:
            return 0

        _RECOVERABLE_KEYWORDS = (
            "harness_document", "harness_structural",
            "harness_json", "harness_interactivity",
            "no_todo", "금지어",
            "stall_limit", "token_limit",
        )

        recovered = 0
        for row in rows:
            desc = row.get("description") or ""
            if not any(kw in desc for kw in _RECOVERABLE_KEYWORDS):
                continue

            is_token_limit = "token_limit" in desc

            node_id = row["id"]
            # 최신 artifact 조회
            try:
                art = await self._db.fetchone(
                    """SELECT av.storage_path
                       FROM artifacts a
                       JOIN artifact_versions av ON av.artifact_id = a.id
                       WHERE a.node_id=? AND av.version_num = a.current_version""",
                    (node_id,),
                )
            except Exception:
                art = None
            if not art or not art.get("storage_path"):
                continue

            # 현재 harness로 재평가 (문서형 기준)
            try:
                from engine.skills.qa.harness import _harness_validate_document
                result = _harness_validate_document(
                    art["storage_path"], row["name"] or "", None,
                )
                still_fail = bool(result.get("structural_failures"))
            except Exception as exc:
                logger.debug(
                    "harness_regression_reeval_error node=%s err=%s",
                    node_id[:8], exc,
                )
                continue

            if still_fail:
                continue

            # 재평가 PASS — 복원
            now = _now()
            try:
                await self._db.execute(
                    """UPDATE nodes
                       SET state='COMPLETED', stall_count=0,
                           updated_at=?, version=version+1
                       WHERE id=? AND state='SUSPENDED'""",
                    (now, node_id),
                )
                # token_limit 사유면 누적 토큰 카운터 리셋 (다음 재시도 공정 시작)
                if is_token_limit:
                    try:
                        await self._db.execute(
                            "DELETE FROM agent_token_usage WHERE node_id=?",
                            (node_id,),
                        )
                    except Exception:
                        pass
                # paired QA 찾기 (qa_pair_node_id 또는 task_pair_node_id 역참조)
                qa_row = await self._db.fetchone(
                    """SELECT id FROM nodes
                       WHERE (qa_pair_node_id=? OR task_pair_node_id=?)
                         AND node_type='QA'
                         AND state IN ('BLOCKED','FAILED','SUSPENDED','INVALID','NOT_STARTED')
                       LIMIT 1""",
                    (node_id, node_id),
                )
                if qa_row:
                    await self._db.execute(
                        """UPDATE nodes
                           SET state='NOT_STARTED', retry_count=0, stall_count=0,
                               updated_at=?, version=version+1
                           WHERE id=?""",
                        (now, qa_row["id"]),
                    )
                recovered += 1
                logger.info(
                    "harness_regression_recovered node=%s name=%s token_limit=%s",
                    node_id[:8], (row["name"] or "")[:40], is_token_limit,
                )
            except Exception as exc:
                logger.warning(
                    "harness_regression_restore_failed node=%s err=%s",
                    node_id[:8], exc,
                )

        if recovered:
            logger.info(
                "harness_regression_recovery_complete count=%d", recovered,
            )
        return recovered

    # ------------------------------------------------------------------
    # 실패 사유 역전파 — project_gotchas → nodes.description
    # ------------------------------------------------------------------

    async def _sync_failure_reasons(self) -> int:
        """SUSPENDED/FAILED 노드 중 description이 비어있는 경우 project_gotchas에서
        가장 최근 실패 사유를 가져와 description에 저장.

        engine이 실패를 gotchas 테이블에 기록은 하지만 node.description 까지
        채우지 않는 코드 경로가 있어, 프런트에서 '실패 사유' 클릭 시 빈 값이 나오는
        문제 보정. 기동 시 1회 실행.
        """
        try:
            result = await self._db.execute(
                """UPDATE nodes
                   SET description = (
                     SELECT json_object(
                       'verdict', 'FAIL',
                       'method', 'gotcha_aggregate',
                       'source', 'project_gotchas',
                       'latest', g.description,
                       'category', g.category
                     )
                     FROM project_gotchas g
                     WHERE (g.source_node_id = nodes.id
                            OR g.source_node_name = nodes.name
                            OR g.source_node_name = '[QA] ' || nodes.name)
                       AND g.project_id = nodes.project_id
                     ORDER BY g.created_at DESC LIMIT 1
                   )
                   WHERE state IN ('SUSPENDED','FAILED')
                     AND (description IS NULL OR description = '')"""
            )
            return int(getattr(result, "rowcount", 0) or 0)
        except Exception as exc:
            logger.warning("sync_failure_reasons_failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Stale QA BLOCKED 해제
    # ------------------------------------------------------------------

    async def _unblock_stale_qa(self) -> int:
        """QA가 BLOCKED 상태로 남아있고, 짝 TASK가 재개/정상 상태면 QA도 NOT_STARTED.

        QA pair guard(executor.py)가 TASK 이상 상태 감지 시 QA를 BLOCKED 처리하는데,
        TASK가 복원된 뒤에는 QA도 다시 흐름에 태워야 한다. 본 메서드는 재시작 시
        스냅샷 기준으로 "TASK가 정상(NOT_STARTED/READY/IN_PROGRESS/COMPLETED)" 인데
        BLOCKED 상태로 방치된 QA를 일괄 NOT_STARTED로 되돌린다.
        """
        # TASK 가 COMPLETED 상태여야만 QA 를 NOT_STARTED 로 돌린다.
        # IN_PROGRESS/READY/NOT_STARTED 에서 풀어주면 TASK 실행 중/대기 중에
        # QA 가 선행해서 낡은 산출물(또는 미완성 산출물)을 검사할 위험.
        rows = await self._db.fetchall(
            """SELECT q.id
               FROM nodes q
               JOIN nodes t ON (
                   t.id = q.task_pair_node_id
                   OR (q.task_pair_node_id IS NULL
                       AND t.node_type='TASK' AND t.dag_id=q.dag_id
                       AND t.project_id=q.project_id
                       AND t.name = SUBSTR(q.name, 6))
               )
               WHERE q.node_type='QA' AND q.state='BLOCKED'
                 AND t.state = 'COMPLETED'
               LIMIT 1000"""
        )
        if not rows:
            return 0
        now = _now()
        ids = [r["id"] for r in rows]
        ph = ",".join(["?"] * len(ids))
        await self._db.execute(
            f"""UPDATE nodes
                SET state='NOT_STARTED', updated_at=?, version=version+1
                WHERE id IN ({ph}) AND state='BLOCKED'""",
            [now] + ids,
        )
        logger.info("startup_qa_unblocked count=%d", len(ids))
        return len(ids)

    # ------------------------------------------------------------------
    # 가비지 노드 정리 (잘못된 splitting으로 생성된 무효 노드)
    # ------------------------------------------------------------------

    async def _cleanup_garbage_nodes(self) -> int:
        """Delete garbage nodes created by buggy DESIGN splitting.

        Detects nodes named 'UI 디자인 시안 (X)' where X is NOT a valid
        Korean group name (e.g. CRUD, 0, quoted strings, descriptions).
        Also cleans up their paired QA nodes and associated edges.

        Safety: only targets the known 'UI 디자인 시안 (*)' pattern and
        validates the group name portion against a strict allowlist of
        Korean-character group names.
        """
        # Find candidate garbage nodes: 'UI 디자인 시안 (...)' or '컴포넌트 라이브러리 (...)' pattern
        # Limit scan to recent nodes only (created in last 7 days) for performance on large DBs
        candidates = await self._db.fetchall(
            """SELECT id, name, qa_pair_node_id, dag_id FROM nodes
               WHERE (name LIKE 'UI 디자인 시안 (%' OR name LIKE '컴포넌트 라이브러리 (%')
                 AND node_type IN ('TASK', 'QA')
                 AND updated_at > datetime('now', '-7 days')
               LIMIT 5000"""
        )
        if not candidates:
            return 0

        from engine.skills.splitting import _parse_screen_groups
        from engine.skills.registry import SkillRegistry

        # 프로젝트별 유효 그룹명 수집
        # - UI 디자인 시안: 화면 목록 정의서에서 GRP-XX 기반 추출
        # - 컴포넌트 라이브러리: spec의 split_categories에서 name 목록 추출
        project_valid_groups: dict[str, set[str]] = {}

        # 컴포넌트 라이브러리 유효 카테고리 (spec 기반, DAG 무관)
        _library_valid_cats: set[str] = set()
        try:
            _lib_spec = SkillRegistry().resolve("컴포넌트 라이브러리", "DESIGN", "TASK")
            if _lib_spec:
                for cat in _lib_spec.get("split_categories", []):
                    _library_valid_cats.add(cat["name"])
        except Exception:
            pass

        for row in candidates:
            dag_id = row["dag_id"]
            if dag_id not in project_valid_groups:
                valid_names: set[str] = set()
                # UI 디자인 시안용: 화면 목록 정의서 로드
                screen_list = await self._db.fetchone(
                    """SELECT av.storage_path FROM artifacts a
                       JOIN artifact_versions av ON a.id=av.artifact_id
                       WHERE a.project_id=(SELECT project_id FROM dags WHERE id=?)
                       AND a.node_id IN (SELECT id FROM nodes WHERE name='화면 목록 정의서'
                           AND project_id=(SELECT project_id FROM dags WHERE id=?))
                       ORDER BY av.version_num DESC LIMIT 1""",
                    (dag_id, dag_id),
                )
                if screen_list and screen_list["storage_path"]:
                    groups = _parse_screen_groups(screen_list["storage_path"])
                    valid_names.update(groups.keys())
                # 컴포넌트 라이브러리용: spec 카테고리 추가
                valid_names.update(_library_valid_cats)
                project_valid_groups[dag_id] = valid_names

        garbage_ids: list[str] = []
        garbage_dag_ids: set[str] = set()
        for row in candidates:
            name: str = row["name"]
            dag_id = row["dag_id"]
            # Extract group name from parentheses
            paren_start = name.find("(")
            paren_end = name.rfind(")")
            if paren_start == -1 or paren_end == -1 or paren_end <= paren_start:
                continue
            group_name = name[paren_start + 1:paren_end].strip()
            # 유효 그룹명 목록과 대조
            valid_groups = project_valid_groups.get(dag_id, set())
            if group_name in valid_groups:
                continue
            # 유효 그룹에 없음 → garbage
            garbage_ids.append(row["id"])
            garbage_dag_ids.add(dag_id)

        # Also collect QA pair nodes of garbage TASK nodes
        qa_ids_to_delete: list[str] = []
        for row in candidates:
            if row["id"] in garbage_ids and row["qa_pair_node_id"]:
                qa_ids_to_delete.append(row["qa_pair_node_id"])
        all_delete_ids = list(set(garbage_ids + qa_ids_to_delete))

        if not all_delete_ids:
            return 0

        ph = ",".join(["?"] * len(all_delete_ids))

        # 잘못된 분할 노드를 SKIPPED 처리 (FK 참조 때문에 삭제 불가)
        await self._db.execute(
            f"UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id IN ({ph})",
            [_now()] + all_delete_ids,
        )

        # 관련 edge 비활성화
        await self._db.execute(
            f"UPDATE edges SET is_active=0 WHERE from_node_id IN ({ph}) OR to_node_id IN ({ph})",
            all_delete_ids + all_delete_ids,
        )

        # 분할 노드가 전부 SKIPPED → 원본 노드 복원 (NOT_STARTED)
        for dag_id in garbage_dag_ids:
            # UI 디자인 시안: 유효한 분할 노드가 남아있는지 확인
            remaining = await self._db.fetchone(
                """SELECT COUNT(*) as cnt FROM nodes
                   WHERE name LIKE 'UI 디자인 시안 (%'
                   AND dag_id=? AND state != 'SKIPPED' AND node_type='TASK'""",
                (dag_id,),
            )
            if not remaining or remaining["cnt"] == 0:
                await self._db.execute(
                    """UPDATE nodes SET state='NOT_STARTED', updated_at=?
                       WHERE name='UI 디자인 시안' AND dag_id=? AND state='SKIPPED' AND node_type='TASK'""",
                    (_now(), dag_id),
                )
                await self._db.execute(
                    """UPDATE nodes SET state='NOT_STARTED', updated_at=?
                       WHERE name='[QA] UI 디자인 시안' AND dag_id=? AND state='SKIPPED' AND node_type='QA'""",
                    (_now(), dag_id),
                )
                logger.info("startup_garbage_original_restored dag=%s type=design", dag_id[:8])

            # 컴포넌트 라이브러리: 유효한 분할 노드가 남아있는지 확인
            remaining_lib = await self._db.fetchone(
                """SELECT COUNT(*) as cnt FROM nodes
                   WHERE name LIKE '컴포넌트 라이브러리 (%'
                   AND dag_id=? AND state != 'SKIPPED' AND node_type='TASK'""",
                (dag_id,),
            )
            if not remaining_lib or remaining_lib["cnt"] == 0:
                await self._db.execute(
                    """UPDATE nodes SET state='NOT_STARTED', updated_at=?
                       WHERE name='컴포넌트 라이브러리' AND dag_id=? AND state='SKIPPED' AND node_type='TASK'""",
                    (_now(), dag_id),
                )
                await self._db.execute(
                    """UPDATE nodes SET state='NOT_STARTED', updated_at=?
                       WHERE name='[QA] 컴포넌트 라이브러리' AND dag_id=? AND state='SKIPPED' AND node_type='QA'""",
                    (_now(), dag_id),
                )
                logger.info("startup_garbage_original_restored dag=%s type=library", dag_id[:8])

        logger.info(
            "startup_garbage_cleanup skipped=%d nodes (task=%d + qa_pairs=%d)",
            len(all_delete_ids), len(garbage_ids), len(qa_ids_to_delete),
        )
        return len(all_delete_ids)

    async def _dedup_split_nodes(self) -> int:
        """같은 DAG에 동일 이름+node_type 노드가 2개 이상이면 1개만 남기고 SKIPPED.

        분할 로직이 중복 실행되어 같은 노드가 여러 개 생성된 경우 정리.
        artifact가 있는 노드를 우선 유지, 없으면 최신(id 기준) 유지.
        """
        dupes = await self._db.fetchall(
            """SELECT dag_id, name, node_type, COUNT(*) as cnt
               FROM nodes
               WHERE state != 'SKIPPED'
               GROUP BY dag_id, name, node_type
               HAVING COUNT(*) > 1
               LIMIT 500"""
        )
        if not dupes:
            return 0

        skipped = 0
        now = _now()
        for d in dupes:
            # 같은 이름의 노드들 조회
            nodes = await self._db.fetchall(
                """SELECT n.id, n.state,
                          (SELECT COUNT(*) FROM artifacts WHERE node_id=n.id) as art_count
                   FROM nodes n
                   WHERE n.dag_id=? AND n.name=? AND n.node_type=? AND n.state != 'SKIPPED'
                   ORDER BY art_count DESC, n.created_at DESC""",
                (d["dag_id"], d["name"], d["node_type"]),
            )
            # 첫 번째(artifact 많은 것/최신)만 유지, 나머지 SKIPPED
            for node in nodes[1:]:
                await self._db.execute(
                    "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
                    (now, node["id"]),
                )
                skipped += 1

        if skipped:
            logger.info("startup_dedup_split skipped=%d duplicate_groups=%d", skipped, len(dupes))
        return skipped

    async def _repair_qa_edges(self) -> None:
        """기존 DAG에서 TASK→downstream TASK edge를 QA→downstream TASK로 보정.

        문제: 이전 DAG는 TASK→downstream TASK edge만 있어서 QA 완료 전에 downstream 실행됨.
        수정: TASK의 QA pair가 있으면 edge를 QA→downstream으로 교체.
        """
        import uuid as _uuid

        # INTRA_PHASE_DEPS에 정의된 의존 관계에서 QA edge가 빠진 것 찾기
        edges = await self._db.fetchall(
            """SELECT e.id, e.dag_id, e.from_node_id, e.to_node_id,
                      fn.name as from_name, fn.qa_pair_node_id as from_qa_id
               FROM edges e
               JOIN nodes fn ON fn.id = e.from_node_id
               WHERE e.is_active=1
               AND fn.node_type='TASK'
               AND fn.qa_pair_node_id IS NOT NULL
               AND e.edge_type IN ('DEPENDS_ON', 'DEPENDS')
               AND NOT EXISTS (
                   SELECT 1 FROM edges e2
                   WHERE e2.from_node_id = fn.qa_pair_node_id
                   AND e2.to_node_id = e.to_node_id
                   AND e2.is_active=1
               )
               LIMIT 500"""
        )

        if not edges:
            return

        now = _now()
        repaired = 0
        for e in edges:
            # TASK→downstream edge를 비활성화
            await self._db.execute(
                "UPDATE edges SET is_active=0 WHERE id=?", (e["id"],)
            )
            # QA→downstream edge 추가
            await self._db.execute(
                "INSERT INTO edges (id, dag_id, from_node_id, to_node_id, edge_type, is_active, created_at) VALUES (?, ?, ?, ?, 'DEPENDS_ON', 1, ?)",
                (str(_uuid.uuid4()), e["dag_id"], e["from_qa_id"], e["to_node_id"], now),
            )
            repaired += 1

        if repaired:
            logger.info("startup_qa_edges_repaired count=%d", repaired)

    # ------------------------------------------------------------------
    # 좀비 노드 처리
    # ------------------------------------------------------------------

    async def _handle_zombie_nodes(self) -> int:
        """
        IN_PROGRESS 상태이지만 heartbeat 5분 이상 없는 노드 처리.
        - retry_count < 3 → NOT_STARTED (clean re-execution)
        - retry_count >= 3 → FAILED (prevent infinite loops)
        Also batch-resets paired QA/TASK nodes.
        Uses batch SQL for performance (hundreds of zombies in <1s).
        """
        rows = await self._db.fetchall(
            f"""SELECT id, name, COALESCE(retry_count, 0) AS retry_count
                FROM nodes
                WHERE state='IN_PROGRESS'
                  AND (last_heartbeat IS NULL
                       OR julianday('now') - julianday(last_heartbeat)
                          > {self.ZOMBIE_THRESHOLD_MINUTES}.0 / 1440.0)"""
        )
        if not rows:
            return 0

        now = _now()

        # Partition into reset vs failed buckets
        reset_ids: list[str] = []
        failed_ids: list[str] = []
        for row in rows:
            retries = row["retry_count"] or 0
            if retries >= 3:
                failed_ids.append(row["id"])
            else:
                reset_ids.append(row["id"])

        # Batch update: FAILED
        if failed_ids:
            ph = ",".join(["?"] * len(failed_ids))
            await self._db.execute(
                f"""UPDATE nodes
                    SET state='FAILED', updated_at=?, version=version+1
                    WHERE id IN ({ph})""",
                [now] + failed_ids,
            )
            logger.warning(
                "startup_zombie_failed_batch count=%d ids=%s",
                len(failed_ids),
                ",".join(i[:8] for i in failed_ids[:5]) + ("..." if len(failed_ids) > 5 else ""),
            )

        # Batch update: NOT_STARTED (retry_count+1)
        if reset_ids:
            ph = ",".join(["?"] * len(reset_ids))
            await self._db.execute(
                f"""UPDATE nodes
                    SET state='NOT_STARTED',
                        retry_count=COALESCE(retry_count,0)+1,
                        updated_at=?, version=version+1
                    WHERE id IN ({ph})""",
                [now] + reset_ids,
            )
            # Batch reset paired QA/TASK nodes
            await self._db.execute(
                f"""UPDATE nodes
                    SET state='NOT_STARTED', updated_at=?, version=version+1
                    WHERE (task_pair_node_id IN ({ph}) OR qa_pair_node_id IN ({ph}))
                      AND state NOT IN ('COMPLETED','SKIPPED')""",
                [now] + reset_ids + reset_ids,
            )
            logger.info(
                "startup_zombie_reset_batch count=%d ids=%s",
                len(reset_ids),
                ",".join(i[:8] for i in reset_ids[:5]) + ("..." if len(reset_ids) > 5 else ""),
            )

        # Stale agent_processes 정리: 서버 재시작이므로 모든 ALIVE/STARTING 레코드 TERMINATED
        try:
            await self._db.execute(
                """UPDATE agent_processes SET status='TERMINATED', updated_at=?
                   WHERE status IN ('ALIVE','STARTING','RETRYING')""",
                [now],
            )
        except Exception:
            pass

        count = len(reset_ids) + len(failed_ids)
        logger.info("startup_zombies_handled total=%d reset=%d failed=%d",
                    count, len(reset_ids), len(failed_ids))
        return count

    # ------------------------------------------------------------------
    # SHUTDOWN_DRAIN → READY 재개
    # ------------------------------------------------------------------

    async def _resume_shutdown_drain(self) -> int:
        """정상 셧다운으로 SUSPENDED된 노드를 READY로 복원 (batch)."""
        rows = await self._db.fetchall(
            """SELECT id FROM nodes
               WHERE state='SUSPENDED'
                 AND suspension_reason='SHUTDOWN_DRAIN'"""
        )
        if not rows:
            return 0
        now = _now()
        ids = [r["id"] for r in rows]
        ph = ",".join(["?"] * len(ids))
        await self._db.execute(
            f"""UPDATE nodes
                SET state='READY', suspension_reason=NULL,
                    updated_at=?, version=version+1
                WHERE id IN ({ph})""",
            [now] + ids,
        )
        logger.info("startup_shutdown_drain_resumed count=%s", len(ids))
        return len(ids)

    # ------------------------------------------------------------------
    # Cascade Invalidation 미처리 배치
    # ------------------------------------------------------------------

    async def _process_cascade_pending(self) -> int:
        """시작 시 남은 invalidation_pending=1 노드 처리 (batch)."""
        rows = await self._db.fetchall(
            "SELECT id FROM nodes WHERE invalidation_pending=1 LIMIT 100"
        )
        if not rows:
            return 0
        now = _now()
        ids = [r["id"] for r in rows]
        ph = ",".join(["?"] * len(ids))
        await self._db.execute(
            f"""UPDATE nodes
                SET state='INVALID', invalidation_pending=0,
                    updated_at=?, version=version+1
                WHERE id IN ({ph}) AND invalidation_pending=1""",
            [now] + ids,
        )
        logger.info("startup_cascade_pending_processed count=%d", len(ids))
        return len(ids)


# ---------------------------------------------------------------------------
# SQLite 백업 스케줄러
# ---------------------------------------------------------------------------

class BackupScheduler:
    """
    매일 03:00 자동 백업 (asyncio.sleep 루프).
    1. WAL 체크포인트
    2. VACUUM INTO backups/platform_YYYYMMDD_HHMMSS.db
    3. integrity_check
    4. SHA-256 체크섬
    5. artifacts tar.gz
    6. 30일 초과 파일 삭제
    """

    def __init__(self, db: DatabaseAdapter, db_path: str, base_dir: str = ".") -> None:
        self._db = db
        self._db_path = db_path
        self._base_dir = Path(base_dir)
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._wait_until_3am()
            try:
                await self._backup()
            except Exception as exc:
                logger.error("backup_failed error=%s", str(exc))

    async def stop(self) -> None:
        self._running = False

    async def _wait_until_3am(self) -> None:
        """다음 03:00까지 대기."""
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target = target + timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        await asyncio.sleep(wait_secs)

    async def _backup(self) -> None:
        backup_dir = self._base_dir / "backups"
        backup_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"platform_{ts}.db"

        # WAL 체크포인트
        await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)", ())

        # VACUUM INTO
        await self._db.execute(f"VACUUM INTO '{backup_path}'", ())

        # integrity_check
        row = await self._db.fetchone("PRAGMA integrity_check", ())
        if row and list(row.values())[0] != "ok":
            logger.error("backup_integrity_failed result=%s", row)
            return

        # SHA-256
        data = backup_path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        logger.info("backup_created path=%s checksum=%s", str(backup_path), checksum[:12])

        # artifacts 압축
        artifacts_dir = self._base_dir / "artifacts"
        if artifacts_dir.exists():
            tar_path = backup_dir / f"artifacts_{ts}.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(artifacts_dir, arcname="artifacts")

        # 30일 초과 파일 삭제
        self._cleanup_old_backups(backup_dir, days=30)

    def _cleanup_old_backups(self, backup_dir: Path, days: int) -> None:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)
        for f in backup_dir.glob("platform_*.db"):
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                logger.info("backup_deleted_old path=%s", str(f))
        for f in backup_dir.glob("artifacts_*.tar.gz"):
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
