"""
engine/lifecycle/engine_version.py
엔진 코드 버전 추적 — 파일 콘텐츠 해시 기반.

Critical 엔진 파일이 변경되면, 영향받는 프로젝트 산출물을 자동 식별하여 재빌드 트리거.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENGINE_ROOT = Path(__file__).resolve().parent.parent

# ── 코드 생성 결과에 영향을 주는 핵심 파일 그룹 ──
TRACKED_FILES: dict[str, list[str]] = {
    "codegen": [
        "skills/codegen/react.py",
        "skills/codegen/page_builder.py",
        "skills/codegen/helpers.py",
        "skills/codegen/frontend_infra.py",
    ],
    "workspace": [
        "workspace/paths.py",
        "workspace/auto_deploy.py",
        "workspace/ui_completeness.py",
        "workspace/api_connector.py",
    ],
    "composition": [
        "composition/recipe_generator.py",
        "composition/renderer.py",
        "composition/registry.py",
    ],
    "testing": [
        "testing/playwright_runner.js",
    ],
}

# 파일 그룹 → 영향받는 노드 이름 패턴 (SQL LIKE)
GROUP_TO_NODE_PATTERNS: dict[str, list[str]] = {
    "codegen": ["%프론트엔드%"],
    "workspace": ["%프론트엔드%"],
    # composition 그룹: composition renderer 경유로 생성되는 산출물만 포함.
    # "%시안%" 은 여기서 제외 — UI 디자인 시안은 LLM chunked HTML 경로로 직접
    # 생성되며 composition/renderer.py 의 컴포넌트 조립 로직을 타지 않음.
    # 과거 "%시안%" 매핑은 renderer.py 변경 시 UI 시안까지 무효화시키는
    # false positive 를 유발 (관계없는 산출물의 2시간+ 재생성 강제).
    "composition": ["%레시피%", "%조립%"],
    "testing": [],  # 테스트 변경은 재빌드 불필요
}


def compute_engine_version() -> dict[str, str]:
    """각 추적 파일 그룹의 해시를 계산.

    Returns:
        {"codegen": "abc123...", "workspace": "def456...", ...}
    """
    versions: dict[str, str] = {}
    for group, files in TRACKED_FILES.items():
        hasher = hashlib.sha256()
        for f in sorted(files):
            fpath = ENGINE_ROOT / f
            if fpath.exists():
                hasher.update(fpath.read_bytes())
        versions[group] = hasher.hexdigest()[:16]
    return versions


def get_stored_version(db_row: dict | None) -> dict[str, str]:
    """DB에서 저장된 버전 파싱."""
    if not db_row:
        return {}
    try:
        return json.loads(db_row.get("value", "{}"))
    except (ValueError, TypeError):
        return {}


def diff_versions(current: dict[str, str], stored: dict[str, str]) -> list[str]:
    """변경된 그룹 목록 반환."""
    changed: list[str] = []
    for group in current:
        if current[group] != stored.get(group):
            changed.append(group)
    return changed


async def check_and_invalidate_on_engine_change(db: Any) -> list[dict]:
    """엔진 코드 변경 시 영향받는 노드 자동 무효화.

    - kv_store에서 이전 엔진 버전 로드
    - 현재 버전과 비교
    - 변경된 그룹의 패턴에 매칭되는 COMPLETED 노드 → INVALID
    - 멱등성 보장: 버전 동일하면 아무 작업 없음

    Returns:
        무효화된 노드 정보 리스트
    """
    from datetime import datetime, timezone

    results: list[dict] = []

    # 1. 현재 엔진 버전 계산
    current = compute_engine_version()

    # 2. 저장된 버전 로드
    try:
        row = await db.fetchone(
            "SELECT value FROM kv_store WHERE key='engine_code_version'"
        )
    except Exception:
        row = None

    stored = get_stored_version(row)

    # 3. 변경 그룹 확인
    changed_groups = diff_versions(current, stored)
    if not changed_groups:
        logger.debug("engine_version_unchanged groups=%s", list(current.keys()))
        return results

    logger.info(
        "engine_version_changed groups=%s current=%s stored=%s",
        changed_groups, current, stored,
    )

    # 4. 모든 프로젝트에서 영향받는 노드 무효화
    now = datetime.now(timezone.utc).isoformat()

    for group in changed_groups:
        patterns = GROUP_TO_NODE_PATTERNS.get(group, [])
        if not patterns:
            continue

        for pattern in patterns:
            # COMPLETED TASK 노드 찾기
            try:
                affected_tasks = await db.fetchall(
                    "SELECT id, name, project_id FROM nodes "
                    "WHERE node_type='TASK' AND state='COMPLETED' "
                    "AND name LIKE ? LIMIT 50",
                    (pattern,),
                )
            except Exception as exc:
                logger.warning(
                    "engine_version_query_failed group=%s pattern=%s error=%s",
                    group, pattern, str(exc),
                )
                continue

            for task in affected_tasks:
                task_id = task["id"]
                task_name = task["name"]
                project_id = task["project_id"]

                try:
                    # TASK → NOT_STARTED (INVALID은 state_machine에서 BLOCKED 전이 불가)
                    await db.execute(
                        "UPDATE nodes SET state='NOT_STARTED', updated_at=?, "
                        "retry_count=0, version=version+1 WHERE id=? AND state='COMPLETED'",
                        (now, task_id),
                    )

                    # QA 쌍 노드 → NOT_STARTED
                    qa_node = await db.fetchone(
                        "SELECT id FROM nodes WHERE task_pair_node_id=? "
                        "AND node_type='QA' AND state='COMPLETED'",
                        (task_id,),
                    )
                    qa_id = None
                    if qa_node:
                        await db.execute(
                            "UPDATE nodes SET state='NOT_STARTED', updated_at=?, "
                            "version=version+1 WHERE id=?",
                            (now, qa_node["id"]),
                        )
                        qa_id = qa_node["id"]

                    results.append({
                        "group": group,
                        "pattern": pattern,
                        "project_id": project_id,
                        "task_id": task_id,
                        "task_name": task_name,
                        "qa_id": qa_id,
                    })

                    logger.info(
                        "engine_version_invalidated group=%s node=%s name='%s' "
                        "project=%s qa=%s",
                        group, task_id[:8], task_name,
                        project_id[:8], qa_id[:8] if qa_id else "none",
                    )
                except Exception as exc:
                    logger.warning(
                        "engine_version_invalidate_failed node=%s error=%s",
                        task_id[:8], str(exc),
                    )

    # 5. 현재 버전을 kv_store에 저장
    try:
        await db.execute(
            "INSERT INTO kv_store (key, value) VALUES ('engine_code_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(current),),
        )
        logger.info("engine_version_stored version=%s", current)
    except Exception as exc:
        logger.warning("engine_version_store_failed error=%s", str(exc))

    if results:
        logger.warning(
            "engine_version_invalidation_complete changed_groups=%s "
            "invalidated_nodes=%d",
            changed_groups, len(results),
        )

    return results
