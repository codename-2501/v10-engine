"""
engine/lifecycle/project_cloner.py
프로젝트 클론 유틸리티.

손상된 프로젝트에서 정상(COMPLETED/SKIPPED) 노드만 추출해
새 인게이지먼트+프로젝트+DAG로 이식.

정책:
  - COMPLETED / SKIPPED → 새 프로젝트에서도 COMPLETED/SKIPPED (artifacts 포함 복사)
  - 그 외 모든 상태 → NOT_STARTED (재실행)
  - split_design 서브노드 (이름에 "(" 포함, TASK/QA, DESIGN, 시안 포함) → 제외
    새 프로젝트에서 split_design이 정상적으로 재생성함
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from engine.db.adapter import DatabaseAdapter
from engine.observability.logger import get_logger

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _is_split_subnode(name: str, node_type: str, phase: str) -> bool:
    """split_design이 만든 서브노드 여부. 클론 시 제외."""
    return (
        "(" in name
        and node_type in ("TASK", "QA")
        and phase == "DESIGN"
        and "시안" in name
    )


async def clone_engagement(
    db: DatabaseAdapter,
    source_engagement_id: str,
    new_name: str | None = None,
) -> dict:
    """
    source_engagement_id 의 프로젝트를 새 인게이지먼트로 클론.

    Returns:
        new_engagement_id, new_project_id, new_dag_id,
        copied_nodes, reset_nodes, skipped_split_nodes, copied_artifacts
    """
    now = _now()

    # ── 1. 원본 데이터 로드 ───────────────────────────────────────────────

    src_eng = await db.fetchone(
        "SELECT * FROM engagements WHERE id=?", (source_engagement_id,)
    )
    if not src_eng:
        raise ValueError(f"Engagement not found: {source_engagement_id}")

    src_proj = await db.fetchone(
        "SELECT * FROM projects WHERE engagement_id=?", (source_engagement_id,)
    )
    if not src_proj:
        raise ValueError("Source project not found")

    src_dag = await db.fetchone(
        "SELECT * FROM dags WHERE project_id=?", (src_proj["id"],)
    )
    if not src_dag:
        raise ValueError("Source DAG not found")

    src_nodes = await db.fetchall(
        "SELECT * FROM nodes WHERE dag_id=?", (src_dag["id"],)
    )
    src_edges = await db.fetchall(
        """SELECT e.* FROM edges e
           JOIN nodes n ON n.id = e.from_node_id
           WHERE n.dag_id=?""",
        (src_dag["id"],),
    )

    # ── 2. 새 ID 생성 및 제외 노드 분류 ────────────────────────────────────

    new_eng_id  = _new_id()
    new_proj_id = _new_id()
    new_dag_id  = _new_id()

    node_id_map: dict[str, str] = {}
    excluded_ids: set[str] = set()

    for n in src_nodes:
        if _is_split_subnode(n["name"], n["node_type"], n["phase"]):
            excluded_ids.add(n["id"])
        else:
            node_id_map[n["id"]] = _new_id()

    # ── 3. 새 인게이지먼트 ─────────────────────────────────────────────────

    await db.execute(
        """INSERT INTO engagements
           (id, name, client_name, intake_submission_id, status,
            global_context, constitution_version_id,
            deadline, priority, component_count, version,
            created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_eng_id,
            new_name or f"{src_eng['name']} (클론)",
            src_eng["client_name"],
            src_eng["intake_submission_id"],
            "ACTIVE",
            src_eng["global_context"],
            src_eng["constitution_version_id"],
            src_eng["deadline"],
            src_eng["priority"],
            src_eng["component_count"],
            0,
            src_eng["created_by"],
            now, now,
        ),
    )

    # ── 4. 새 프로젝트 ─────────────────────────────────────────────────────

    await db.execute(
        """INSERT INTO projects
           (id, name, client_name, project_type, status,
            global_context, constitution_version_id, template_id,
            intake_submission_id, engagement_id, component_type,
            deadline, priority, phase, version, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_proj_id,
            src_proj["name"],
            src_proj["client_name"],
            src_proj["project_type"],
            "ACTIVE",
            src_proj["global_context"],
            src_proj["constitution_version_id"],
            src_proj["template_id"],
            src_proj["intake_submission_id"],
            new_eng_id,
            src_proj["component_type"],
            src_proj["deadline"],
            src_proj["priority"],
            src_proj["phase"],
            0,
            src_proj["created_by"],
            now, now,
        ),
    )

    # ── 5. 새 DAG ─────────────────────────────────────────────────────────

    old_topo: list[str] = []
    if src_dag["topo_order"]:
        try:
            old_topo = json.loads(src_dag["topo_order"])
        except Exception:
            old_topo = []

    new_topo = [node_id_map[nid] for nid in old_topo if nid in node_id_map]

    _KEEP = {"COMPLETED", "SKIPPED"}
    total_nodes    = len(node_id_map)
    completed_cnt  = sum(1 for n in src_nodes if n["id"] in node_id_map and n["state"] in _KEEP)

    await db.execute(
        """INSERT INTO dags
           (id, project_id, template_id, status,
            total_nodes, completed_nodes, current_phase,
            topo_order, version, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_dag_id,
            new_proj_id,
            src_dag["template_id"],
            "RUNNING",
            total_nodes,
            completed_cnt,
            src_dag["current_phase"],
            json.dumps(new_topo) if new_topo else None,
            0,
            now, now,
        ),
    )

    # ── 6. 노드 복사 (pair 참조 없이 먼저 INSERT, 이후 UPDATE) ───────────

    copied_nodes = 0
    reset_nodes  = 0
    pair_updates: list[tuple] = []  # (new_nid, new_qa_pair, new_task_pair)

    for n in src_nodes:
        if n["id"] not in node_id_map:
            continue

        new_nid       = node_id_map[n["id"]]
        new_qa_pair   = node_id_map.get(n["qa_pair_node_id"])  if n["qa_pair_node_id"]  else None
        new_task_pair = node_id_map.get(n["task_pair_node_id"]) if n["task_pair_node_id"] else None

        new_state = n["state"] if n["state"] in _KEEP else "NOT_STARTED"
        if new_state in _KEEP:
            copied_nodes += 1
        else:
            reset_nodes  += 1

        # pair 없이 먼저 INSERT (FK 순서 충돌 방지)
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name, description,
                state, qa_pair_node_id, task_pair_node_id,
                gate_auto_approve, gate_trigger_type,
                assigned_model, constitution_version_id,
                retry_count, max_retries,
                suspension_reason, invalidation_pending,
                priority, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_nid, new_dag_id, new_proj_id,
                n["node_type"], n["phase"], n["name"], n["description"],
                new_state, None, None,  # pair는 나중에 UPDATE
                n["gate_auto_approve"], n["gate_trigger_type"],
                n["assigned_model"], n["constitution_version_id"],
                0 if new_state == "NOT_STARTED" else n["retry_count"],
                n["max_retries"],
                None, 0,
                n["priority"], 0,
                now, now,
            ),
        )

        if new_qa_pair or new_task_pair:
            pair_updates.append((new_qa_pair, new_task_pair, new_nid))

    # 전체 노드 INSERT 완료 후 pair 참조 UPDATE
    for (qa_pair, task_pair, nid) in pair_updates:
        await db.execute(
            "UPDATE nodes SET qa_pair_node_id=?, task_pair_node_id=?, updated_at=? WHERE id=?",
            (qa_pair, task_pair, now, nid),
        )

    # ── 7. 엣지 복사 ──────────────────────────────────────────────────────

    for e in src_edges:
        from_id = node_id_map.get(e["from_node_id"])
        to_id   = node_id_map.get(e["to_node_id"])
        if not from_id or not to_id:
            continue

        await db.execute(
            """INSERT OR IGNORE INTO edges
               (id, from_node_id, to_node_id, dag_id, edge_type, created_at)
               VALUES (?,?,?,?,?,?)""",
            (_new_id(), from_id, to_id, new_dag_id, e["edge_type"], now),
        )

    # ── 8. Artifacts 복사 (COMPLETED/SKIPPED 노드만) ─────────────────────

    copied_artifacts = 0

    for n in src_nodes:
        if n["id"] not in node_id_map or n["state"] not in _KEEP:
            continue

        new_nid = node_id_map[n["id"]]
        arts = await db.fetchall(
            "SELECT * FROM artifacts WHERE node_id=?", (n["id"],)
        )

        for a in arts:
            new_art_id = _new_id()
            await db.execute(
                """INSERT INTO artifacts
                   (id, node_id, project_id, artifact_type, file_type,
                    current_version, is_invalidated, version,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_art_id, new_nid, new_proj_id,
                    a["artifact_type"], a["file_type"],
                    a["current_version"], 0, 0,
                    now, now,
                ),
            )

            avs = await db.fetchall(
                "SELECT * FROM artifact_versions WHERE artifact_id=? ORDER BY version_num",
                (a["id"],),
            )
            for av in avs:
                await db.execute(
                    """INSERT INTO artifact_versions
                       (id, artifact_id, version_num, storage_path,
                        content_hash, size_bytes, is_qa_approved,
                        created_by, created_at, file_manifest)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _new_id(), new_art_id, av["version_num"],
                        av["storage_path"],
                        av["content_hash"], av["size_bytes"],
                        av["is_qa_approved"],
                        av["created_by"], now,
                        av["file_manifest"],
                    ),
                )
                copied_artifacts += 1

    logger.info(
        "project_clone_complete src=%s new_eng=%s copied=%d reset=%d excluded=%d artifacts=%d",
        source_engagement_id, new_eng_id,
        copied_nodes, reset_nodes, len(excluded_ids), copied_artifacts,
    )

    return {
        "new_engagement_id": new_eng_id,
        "new_project_id":    new_proj_id,
        "new_dag_id":        new_dag_id,
        "copied_nodes":      copied_nodes,
        "reset_nodes":       reset_nodes,
        "skipped_split_nodes": len(excluded_ids),
        "copied_artifacts":  copied_artifacts,
    }
