"""
engine/lifecycle/dag_repair.py
DAG 손상 복구 유틸리티.

repair_pair_references: 이름 매칭으로 끊긴 qa_pair / task_pair 참조 복원.
resume_api_suspended: API 한도로 SUSPENDED된 노드 → BLOCKED, DAG PAUSED → RUNNING.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.db.adapter import DatabaseAdapter
from engine.observability.logger import get_logger

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def repair_pair_references(db: DatabaseAdapter, dag_id: str) -> dict:
    """
    끊긴 qa_pair_node_id / task_pair_node_id 를 이름 매칭으로 복원.

    매칭 규칙:
      TASK "UI 디자인 시안 (xxx)" ↔ QA "[QA] UI 디자인 시안 (xxx)"
      → QA 이름에서 "[QA] " 제거 후 TASK 이름과 비교.
    """
    now = _now()

    # 1. 끊긴 qa_pair_node_id를 가진 TASK 노드 조회
    broken_tasks = await db.fetchall(
        """SELECT id, name FROM nodes
           WHERE dag_id=?
             AND node_type='TASK'
             AND qa_pair_node_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM nodes q WHERE q.id = qa_pair_node_id)""",
        (dag_id,),
    )

    # 2. 끊긴 task_pair_node_id를 가진 QA 노드 조회
    broken_qas = await db.fetchall(
        """SELECT id, name FROM nodes
           WHERE dag_id=?
             AND node_type='QA'
             AND task_pair_node_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.id = task_pair_node_id)""",
        (dag_id,),
    )

    # 이름 → id 맵 구성
    # TASK: name → id
    task_name_map: dict[str, str] = {r["name"]: r["id"] for r in broken_tasks}
    # QA: stripped_name → (id, original_name)
    qa_name_map: dict[str, str] = {}
    for r in broken_qas:
        stripped = r["name"].removeprefix("[QA] ")
        qa_name_map[stripped] = r["id"]

    # 3. 매칭 후 UPDATE
    task_fixed = 0
    qa_fixed = 0

    for task_name, task_id in task_name_map.items():
        qa_id = qa_name_map.get(task_name)
        if qa_id:
            await db.execute(
                "UPDATE nodes SET qa_pair_node_id=?, updated_at=? WHERE id=?",
                (qa_id, now, task_id),
            )
            task_fixed += 1

    for stripped_name, qa_id in qa_name_map.items():
        task_id = task_name_map.get(stripped_name)
        if task_id:
            await db.execute(
                "UPDATE nodes SET task_pair_node_id=?, updated_at=? WHERE id=?",
                (task_id, now, qa_id),
            )
            qa_fixed += 1

    logger.info(
        "dag_repair_pair_references dag_id=%s task_fixed=%d qa_fixed=%d broken_tasks=%d broken_qas=%d",
        dag_id, task_fixed, qa_fixed, len(broken_tasks), len(broken_qas),
    )
    return {
        "broken_tasks": len(broken_tasks),
        "broken_qas": len(broken_qas),
        "task_fixed": task_fixed,
        "qa_fixed": qa_fixed,
    }


async def resume_api_suspended(db: DatabaseAdapter, dag_id: str) -> dict:
    """
    API 한도로 SUSPENDED된 노드를 BLOCKED으로 전환 후 DAG를 RUNNING으로 재개.
    DAGAdvancer가 자연스럽게 deps 체크 → READY → 실행.
    """
    now = _now()

    rows = await db.fetchall(
        """SELECT id FROM nodes
           WHERE dag_id=?
             AND state='SUSPENDED'
             AND suspension_reason LIKE 'API 한도%'""",
        (dag_id,),
    )
    if not rows:
        logger.info("resume_api_suspended dag_id=%s no_suspended_nodes", dag_id)
        return {"resumed": 0}

    ids = [r["id"] for r in rows]
    ph = ",".join(["?"] * len(ids))
    await db.execute(
        f"""UPDATE nodes
            SET state='BLOCKED', suspension_reason=NULL,
                updated_at=?, version=version+1
            WHERE id IN ({ph})""",
        [now] + ids,
    )

    # DAG PAUSED → RUNNING
    await db.execute(
        "UPDATE dags SET status='RUNNING', updated_at=? WHERE id=?",
        (now, dag_id),
    )

    logger.info(
        "resume_api_suspended dag_id=%s resumed=%d dag_status=RUNNING",
        dag_id, len(ids),
    )
    return {"resumed": len(ids)}
