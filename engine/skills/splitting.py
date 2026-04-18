from __future__ import annotations

import json
import logging
import re
import uuid as _uuid
from typing import Any

from engine.core.dag_advancer import NodeSnapshot
from engine.skills.utils import _now

logger = logging.getLogger(__name__)

# SCR 테이블 파싱 정규식 (executor.py와 동일)
_SCR_TABLE_RE = re.compile(
    r'SCR-(\d{3})\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+)',
)


# ---------------------------------------------------------------------------
# 페이지 그룹 분할 — 대규모 프로젝트 독립 노드 생성
# ---------------------------------------------------------------------------

_SLUG_PREFIX_LABELS = {
    "admin": "관리자",
    "bo": "백오피스",
    "caregiver": "요양보호사",
    "carer": "요양보호사",
    "elder": "어르신",
    "family": "보호자",
    "shop": "쇼핑",
    "community": "커뮤니티",
    "health": "건강관리",
    "hospital": "병원",
    "sitter": "돌봄",
    "pet": "반려동물",
    "booking": "예약",
    "dm": "메시지",
    "rating": "평가",
}


def _group_pages_for_splitting(
    results: list,
    max_per_group: int = 4,
    min_group_size: int = 2,
) -> dict[str, list]:
    """페이지를 URL prefix 기반으로 그룹핑.

    Returns: {"관리자": [result, ...], "기타 1": [result, ...]}
    """
    groups: dict[str, list] = {}

    for r in results:
        slug = r.page_slug or ""
        # 하이픈 구분 prefix 우선, 없으면 전체 slug로 prefix 매칭 시도
        prefix = slug.split("-")[0] if "-" in slug else slug
        label = _SLUG_PREFIX_LABELS.get(prefix, "")

        if label:
            groups.setdefault(label, []).append(r)
        else:
            groups.setdefault("_ungrouped", []).append(r)

    # 소그룹(< min_group_size) → _ungrouped에 병합
    merged = {}
    ungrouped = list(groups.pop("_ungrouped", []))
    for label, pages in groups.items():
        if len(pages) < min_group_size:
            ungrouped.extend(pages)
        else:
            merged[label] = pages

    # 대그룹(> max_per_group) → 청크 분할
    final: dict[str, list] = {}
    for label, pages in merged.items():
        if len(pages) <= max_per_group:
            final[label] = pages
        else:
            for i in range(0, len(pages), max_per_group):
                chunk = pages[i:i + max_per_group]
                suffix = f" {i // max_per_group + 1}" if len(pages) > max_per_group else ""
                final[f"{label}{suffix}"] = chunk

    # ungrouped → max_per_group씩 분할
    if ungrouped:
        for i in range(0, len(ungrouped), max_per_group):
            chunk = ungrouped[i:i + max_per_group]
            idx = i // max_per_group + 1
            label = f"기타 {idx}" if len(ungrouped) > max_per_group else "기타"
            final[label] = chunk

    return final


async def _split_frontend_component_nodes(
    db: Any, node: "NodeSnapshot", results: list,
) -> None:
    """페이지 조립 완료 후 '프론트엔드 컴포넌트 구현'을 페이지 그룹별 독립 노드로 분할.

    - 원본 TASK + QA → SKIPPED
    - 그룹별 TASK + QA 노드 INSERT
    - 엣지: 공통 인프라 → 각 TASK → QA → GATE
    """
    import uuid as _uuid

    now = _now()

    # 원본 "프론트엔드 컴포넌트 구현" 노드 찾기
    original_task = await db.fetchone(
        """SELECT n.id, n.dag_id, n.assigned_model, n.qa_pair_node_id
           FROM nodes n JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id=? AND n.name='프론트엔드 컴포넌트 구현'
             AND n.node_type='TASK' AND n.phase='BUILD' LIMIT 1""",
        (node.project_id,),
    )
    if not original_task:
        logger.warning("split_frontend_no_original project=%s", node.project_id)
        return

    dag_id = original_task["dag_id"]
    model = original_task["assigned_model"] or "sonnet"
    original_task_id = original_task["id"]
    original_qa_id = original_task["qa_pair_node_id"]

    # 공통 인프라 노드 ID (엣지 연결용)
    infra_node = await db.fetchone(
        """SELECT n.id FROM nodes n JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id=? AND n.name='프론트엔드 공통 인프라'
             AND n.node_type='TASK' LIMIT 1""",
        (node.project_id,),
    )
    infra_node_id = infra_node["id"] if infra_node else None

    # BUILD → VERIFY GATE 노드 ID (QA → GATE 엣지용)
    gate_node = await db.fetchone(
        """SELECT n.id FROM nodes n
           WHERE n.dag_id=? AND n.name LIKE '%BUILD%VERIFY%'
             AND n.node_type='GATE' LIMIT 1""",
        (dag_id,),
    )
    gate_node_id = gate_node["id"] if gate_node else None

    # 페이지 그룹 분할
    page_groups = _group_pages_for_splitting(results)

    if len(page_groups) <= 1:
        logger.info("split_frontend_skipped project=%s groups=1 (분할 불필요)", node.project_id)
        return

    # 중복 분할 방지: 이미 분할 노드가 존재하면 스킵
    existing_split = await db.fetchone(
        """SELECT COUNT(*) as cnt FROM nodes
           WHERE dag_id=? AND name LIKE '프론트엔드 컴포넌트 구현 (%'
           AND node_type='TASK' AND state != 'SKIPPED'""",
        (dag_id,),
    )
    if existing_split and existing_split["cnt"] > 0:
        logger.info("split_frontend_already_done dag=%s existing=%d", dag_id[:8], existing_split["cnt"])
        return

    # 원본 노드 SKIPPED
    await db.execute(
        "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
        (now, original_task_id),
    )
    if original_qa_id:
        await db.execute(
            "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
            (now, original_qa_id),
        )

    # 그룹별 노드 생성
    created_count = 0
    for group_name, pages in page_groups.items():
        task_id = str(_uuid.uuid4())
        qa_id = str(_uuid.uuid4())
        node_name = f"프론트엔드 컴포넌트 구현 ({group_name})"

        # TASK 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, task_pair_node_id,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'TASK', 'BUILD', ?, 'NOT_STARTED', ?, NULL, ?, ?, 0)""",
            (task_id, dag_id, node.project_id, node_name, model, now, now),
        )

        # QA 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, task_pair_node_id,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'QA', 'BUILD', ?, 'NOT_STARTED', ?, ?, ?, ?, 0)""",
            (qa_id, dag_id, node.project_id, f"[QA] {node_name}", model,
             task_id, now, now),
        )

        # TASK ↔ QA 양방향
        await db.execute(
            "UPDATE nodes SET qa_pair_node_id=? WHERE id=?",
            (qa_id, task_id),
        )

        # 엣지: 공통 인프라 → TASK
        if infra_node_id:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'DEPENDS_ON', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, infra_node_id, task_id, now),
            )

        # 엣지: TASK → QA
        await db.execute(
            """INSERT OR IGNORE INTO edges
               (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
               VALUES (?, ?, ?, ?, 'QA_PAIR', ?, 1)""",
            (str(_uuid.uuid4()), dag_id, task_id, qa_id, now),
        )

        # 엣지: QA → GATE
        if gate_node_id:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'GATE_WAIT', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, qa_id, gate_node_id, now),
            )

        # 페이지 슬러그 저장 (executor가 이 노드 실행 시 해당 페이지만 로드)
        page_slugs = [r.page_slug for r in pages]
        await db.execute(
            "UPDATE nodes SET description=? WHERE id=?",
            (json.dumps({"page_slugs": page_slugs}, ensure_ascii=False), task_id),
        )

        created_count += 1
        logger.info(
            "split_frontend_node_created group=%s pages=%d task=%s",
            group_name, len(pages), task_id[:8],
        )

    # DAG total_nodes 업데이트
    new_nodes = created_count * 2  # TASK + QA per group
    await db.execute(
        "UPDATE dags SET total_nodes = total_nodes + ? - 2, updated_at=? WHERE id=?",
        (new_nodes, now, dag_id),
    )

    logger.info(
        "split_frontend_complete project=%s groups=%d nodes=%d original_skipped=%s",
        node.project_id, created_count, new_nodes, original_task_id[:8],
    )


# ---------------------------------------------------------------------------
# DESIGN 단계 분할 — "UI 디자인 시안" 대규모 화면 프로젝트 분할
# ---------------------------------------------------------------------------

def _parse_screen_groups(content: str) -> dict[str, list[tuple]]:
    """화면 목록 정의서 내용에서 SCR-XXX 항목만 파싱하여 그룹핑.

    Grouping strategy:
      1. 3번째 컬럼에서 GRP-XX 패턴이 있는 행만 파싱 (다른 테이블 행 제외)
      2. GRP-XX 뒤의 텍스트를 그룹명으로 사용 (예: "GRP-01 공통" → "공통")
      3. GRP 패턴 없는 행은 SCR 번호 십의 자리 기준 자동 그룹핑 (폴백)

    Validation:
      - SCR-XXX 패턴이 있는 행만 파싱
      - 그룹당 최소 1개 SCR 항목 필수
      - 총 그룹 수 10개 상한

    Returns: {"공통": [(SCR-001, name, url, type), ...], ...}
    """
    import re as _rg

    # GRP-XX 패턴으로 그룹명 직접 추출
    _GRP_RE = _rg.compile(r'GRP-\d+\s*(.*)')

    screens_with_grp: list[tuple] = []  # (scr_id, name, url, group_name)
    screens_no_grp: list[tuple] = []    # GRP 없는 행 (폴백용)

    for m in _SCR_TABLE_RE.finditer(content):
        scr_num = int(m.group(1))
        scr_id = f"SCR-{m.group(1)}"
        name = m.group(2).strip()
        col3 = m.group(3).strip()
        col4 = m.group(4).strip().rstrip("|｜ ")

        # 3번째 컬럼에서 GRP-XX 패턴 찾기
        grp_match = _GRP_RE.match(col3)
        if grp_match:
            grp_name = grp_match.group(1).strip() or "기타"
            screens_with_grp.append((scr_id, name, col4, grp_name))
        else:
            screens_no_grp.append((scr_id, name, col3, col4, scr_num))

    # GRP 패턴이 있는 행이 충분하면 그것만 사용
    if len(screens_with_grp) >= 5:
        named_groups: dict[str, list[tuple]] = {}
        for scr_id, name, url, grp_name in screens_with_grp:
            named_groups.setdefault(grp_name, []).append((scr_id, name, url, grp_name))
        # 그룹 수 상한
        _MAX_GROUPS = 10
        if len(named_groups) > _MAX_GROUPS:
            sorted_groups = sorted(named_groups.items(), key=lambda x: -len(x[1]))
            keep = dict(sorted_groups[:_MAX_GROUPS - 1])
            others = []
            for grp_name, members in sorted_groups[_MAX_GROUPS - 1:]:
                others.extend(members)
            if others:
                keep["기타"] = others
            named_groups = keep
        return named_groups

    # 폴백: GRP 패턴 없으면 SCR 번호 십의 자리 기준 자동 그룹핑
    all_screens = [(s[0], s[1], s[2], s[3], s[4]) for s in screens_no_grp]
    if not all_screens:
        return {}

    grp_map: dict[int, list[tuple]] = {}
    for scr in all_screens:
        scr_num = scr[4]
        grp_key = (scr_num - 1) // 10 + 1
        grp_map.setdefault(grp_key, []).append(scr[:4])

    _MAX_GROUPS = 10
    named_groups = {}
    for _grp_key in sorted(grp_map.keys()):
        members = grp_map[_grp_key]
        label = f"그룹 {_grp_key}"
        named_groups[label] = members

    # 그룹 수가 상한 초과 → 소그룹을 "기타"로 병합
    if len(named_groups) > _MAX_GROUPS:
        sorted_groups = sorted(named_groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        keep = dict(sorted_groups[:_MAX_GROUPS - 1])
        overflow: list[tuple] = []
        for label, members in sorted_groups[_MAX_GROUPS - 1:]:
            overflow.extend(members)
        if overflow:
            keep["기타"] = overflow
        named_groups = keep

    return named_groups


async def _count_screens_from_artifact(db: Any, project_id: str) -> int:
    """화면 목록 정의서에서 SCR-XXX 항목 수를 카운트."""
    try:
        row = await db.fetchone(
            """SELECT av.storage_path FROM artifacts a
               JOIN artifact_versions av ON a.id=av.artifact_id
               WHERE a.project_id=? AND a.node_id IN
                 (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
               AND av.version_num = a.current_version""",
            (project_id, project_id),
        )
        if not row or not row["storage_path"]:
            return 0
        return len(_SCR_TABLE_RE.findall(row["storage_path"]))
    except Exception:
        return 0


async def split_design_task_by_group(
    db: Any, project_id: str, node_id: str, dag_id: str,
) -> int:
    """Split a DESIGN 'UI 디자인 시안' task into sub-tasks by screen group.

    Reads 화면 목록 정의서 to find screen groups (유형 컬럼 기준).
    Creates one sub-task per group:
      - "UI 디자인 시안 (공통)" for 공통 screens
      - "UI 디자인 시안 (보호자·어르신)" for 보호자 screens
      - etc.

    Each sub-task gets only its group's screens in the prompt via description metadata.

    Returns number of sub-tasks created (0 if splitting not needed).
    """
    now = _now()

    # 1. 화면 목록 정의서 로드
    row = await db.fetchone(
        """SELECT av.storage_path FROM artifacts a
           JOIN artifact_versions av ON a.id=av.artifact_id
           WHERE a.project_id=? AND a.node_id IN
             (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
           AND av.version_num = a.current_version""",
        (project_id, project_id),
    )
    if not row or not row["storage_path"]:
        logger.info("split_design_no_screen_list project=%s", project_id)
        return 0

    # 2. 화면 그룹 파싱
    screen_groups = _parse_screen_groups(row["storage_path"])
    total_screens = sum(len(v) for v in screen_groups.values())

    # 3. 중복 분할 방지: 이미 분할 노드가 존재하면 스킵
    existing_split = await db.fetchone(
        """SELECT COUNT(*) as cnt FROM nodes
           WHERE dag_id=? AND name LIKE 'UI 디자인 시안 (%'
           AND node_type='TASK' AND state != 'SKIPPED'""",
        (dag_id,),
    )
    if existing_split and existing_split["cnt"] > 0:
        logger.info("split_design_already_done dag=%s existing=%d", dag_id[:8], existing_split["cnt"])
        return 0

    # 4. 분할 불필요 조건: 1그룹 이하 또는 총 10개 이하
    if len(screen_groups) <= 1 or total_screens <= 10:
        logger.info(
            "split_design_skipped project=%s groups=%d screens=%d",
            project_id, len(screen_groups), total_screens,
        )
        return 0

    # 4. 원본 노드 정보 로드
    original = await db.fetchone(
        "SELECT id, dag_id, assigned_model, qa_pair_node_id FROM nodes WHERE id=?",
        (node_id,),
    )
    if not original:
        logger.warning("split_design_no_original node=%s", node_id)
        return 0

    model = original["assigned_model"] or "sonnet"
    original_qa_id = original["qa_pair_node_id"]

    # 5. 업스트림 노드 찾기 (원본 TASK로 향하는 엣지의 from 노드들)
    upstream_rows = await db.fetchall(
        "SELECT from_node_id FROM edges WHERE to_node_id=? AND dag_id=? AND is_active=1",
        (node_id, dag_id),
    )
    upstream_ids = [r["from_node_id"] for r in upstream_rows] if upstream_rows else []

    # 6. 다운스트림 노드 찾기 (원본 QA에서 나가는 엣지의 to 노드들)
    downstream_ids = []
    if original_qa_id:
        downstream_rows = await db.fetchall(
            "SELECT to_node_id FROM edges WHERE from_node_id=? AND dag_id=? AND is_active=1",
            (original_qa_id, dag_id),
        )
        downstream_ids = [r["to_node_id"] for r in downstream_rows] if downstream_rows else []

    # GATE 노드 ID (fallback: phase='DESIGN' GATE)
    if not downstream_ids:
        gate_node = await db.fetchone(
            "SELECT id FROM nodes WHERE dag_id=? AND node_type='GATE' AND phase='DESIGN' LIMIT 1",
            (dag_id,),
        )
        if gate_node:
            downstream_ids = [gate_node["id"]]

    # 7. 원본 TASK + QA → SKIPPED
    await db.execute(
        "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
        (now, node_id),
    )
    if original_qa_id:
        await db.execute(
            "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
            (now, original_qa_id),
        )

    # 8. 그룹별 서브노드 생성
    created_count = 0
    for group_name, screens in screen_groups.items():
        task_id = str(_uuid.uuid4())
        qa_id = str(_uuid.uuid4())
        node_name = f"UI 디자인 시안 ({group_name})"

        # 서브 TASK에 전달할 화면 목록 메타데이터
        screen_meta = {
            "_design_split_group": group_name,
            "_design_split_screens": [
                {"id": s[0], "name": s[1], "url": s[2], "type": s[3]}
                for s in screens
            ],
        }

        # TASK 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, qa_pair_node_id, description,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'TASK', 'DESIGN', ?, 'NOT_STARTED', ?, NULL, ?, ?, ?, 0)""",
            (task_id, dag_id, project_id, node_name, model,
             json.dumps(screen_meta, ensure_ascii=False), now, now),
        )

        # QA 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, task_pair_node_id,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'QA', 'DESIGN', ?, 'NOT_STARTED', ?, ?, ?, ?, 0)""",
            (qa_id, dag_id, project_id, f"[QA] {node_name}", model,
             task_id, now, now),
        )

        # TASK ↔ QA 양방향
        await db.execute(
            "UPDATE nodes SET qa_pair_node_id=? WHERE id=?",
            (qa_id, task_id),
        )

        # 엣지: upstream → sub-TASK
        for up_id in upstream_ids:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'DEPENDS_ON', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, up_id, task_id, now),
            )

        # 엣지: TASK → QA
        await db.execute(
            """INSERT OR IGNORE INTO edges
               (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
               VALUES (?, ?, ?, ?, 'QA_PAIR', ?, 1)""",
            (str(_uuid.uuid4()), dag_id, task_id, qa_id, now),
        )

        # 엣지: QA → downstream (GATE 등)
        for down_id in downstream_ids:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'GATE_WAIT', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, qa_id, down_id, now),
            )

        created_count += 1
        logger.info(
            "split_design_node_created group=%s screens=%d task=%s",
            group_name, len(screens), task_id[:8],
        )

    # 9. DAG total_nodes 업데이트
    new_nodes = created_count * 2  # TASK + QA per group
    await db.execute(
        "UPDATE dags SET total_nodes = total_nodes + ? - 2, updated_at=? WHERE id=?",
        (new_nodes, now, dag_id),
    )

    logger.info(
        "split_design_complete project=%s groups=%d nodes=%d original_skipped=%s",
        project_id, created_count, new_nodes, node_id[:8],
    )

    return created_count


# ---------------------------------------------------------------------------
# DESIGN 단계 분할 — "컴포넌트 라이브러리" 카테고리별 독립 노드 생성
# ---------------------------------------------------------------------------

# project_type 별 부적합 카테고리 (split 시점에 SKIPPED 처리)
# size_estimator 의 ProjectType (mlops/data/app/si/mixed) 와 1:1 매칭.
# - app (모바일/웹/소비자 앱): 운영시스템/관제 카테고리 부적합
# - SI/MLOps/Data: 모든 카테고리 적합 (보수적 default)
# - mixed: 알 수 없으면 보수적으로 모두 진행
_INAPPROPRIATE_CATEGORIES_BY_TYPE: dict[str, set[str]] = {
    "app":    {"monitoring", "production", "mlops", "ml", "ops"},
    "mixed":  set(),
    "si":     set(),
    "mlops":  set(),
    "data":   set(),
}


async def _filter_categories_by_project_type(
    db: Any, project_id: str, categories: list[dict],
) -> list[dict]:
    """SizeProfile.project_type 기준으로 부적합 카테고리 필터링.

    raw_json 에서 project_type 추출 → _INAPPROPRIATE_CATEGORIES_BY_TYPE 매칭.
    실패 시 원본 그대로 반환 (fail-safe).
    """
    try:
        from engine.intake.size_estimator import estimate_size
        import json as _json_pf
        eng_row = await db.fetchone(
            """SELECT e.global_context FROM engagements e
               JOIN projects p ON p.engagement_id=e.id WHERE p.id=?""",
            (project_id,),
        )
        if not eng_row or not eng_row.get("global_context"):
            return categories
        gctx = _json_pf.loads(eng_row["global_context"])
        if not isinstance(gctx, dict):
            return categories
        profile = estimate_size(gctx)
        ptype = (profile.project_type or "mixed").lower()
        bad = _INAPPROPRIATE_CATEGORIES_BY_TYPE.get(ptype, set())
        if not bad:
            return categories
        filtered = [
            c for c in categories
            if str(c.get("name", "")).lower() not in bad
        ]
        if len(filtered) < len(categories):
            removed = sorted(
                str(c.get("name", "")).lower() for c in categories
                if str(c.get("name", "")).lower() in bad
            )
            logger.info(
                "split_library_filter_inappropriate project=%s ptype=%s removed=%s kept=%d/%d",
                project_id, ptype, removed, len(filtered), len(categories),
            )
        return filtered
    except Exception:
        return categories


async def split_component_library_by_category(
    db: Any, project_id: str, node_id: str, dag_id: str,
) -> int:
    """Split a DESIGN '컴포넌트 라이브러리' task into sub-tasks by category.

    Reads split_categories from spec YAML.
    Creates one sub-task per category:
      - "컴포넌트 라이브러리 (layout)" for layout components
      - "컴포넌트 라이브러리 (content)" for content components
      - etc.

    Returns number of sub-tasks created (0 if splitting not needed).
    """
    from engine.skills.registry import SkillRegistry

    now = _now()

    # 1. spec 로드 → split_categories 확인
    spec = SkillRegistry().resolve("컴포넌트 라이브러리", "DESIGN", "TASK")
    if not spec:
        logger.info("split_library_no_spec project=%s", project_id)
        return 0

    # S13: domain_profile 에 component_categories 정의됐으면 우선 사용
    # (intake 에서 S11-A1 로 저장된 _domain_profile_data 참조)
    categories = None
    try:
        eng_row = await db.fetchone(
            """SELECT e.global_context FROM engagements e
            JOIN projects p ON p.engagement_id=e.id WHERE p.id=?""",
            (project_id,),
        )
        if eng_row and eng_row.get("global_context"):
            import json as _json_s13
            gctx = _json_s13.loads(eng_row["global_context"])
            profile_data = gctx.get("_domain_profile_data") if isinstance(gctx, dict) else None
            if profile_data and profile_data.get("component_categories"):
                categories = profile_data["component_categories"]
                logger.info(
                    "split_library_profile_override project=%s profile_cats=%d",
                    project_id, len(categories),
                )
    except Exception as _pe:
        logger.warning("split_library_profile_load_failed: %s", _pe)

    # Fallback: spec.split_categories
    if not categories:
        categories = spec.get("split_categories", [])

    # ── 부적합 카테고리 필터링 (project_type 기반) ──
    # domain_profile 미정의 프로젝트 (모바일/웹/소비자 앱) 에서 monitoring/production
    # 같은 운영시스템 카테고리가 균등 분할되어 LLM이 부적합 컴포넌트 생성 → 영구 FAILED.
    # SizeProfile.project_type 기준으로 부적합 카테고리 사전 제거.
    if categories:
        try:
            categories = await _filter_categories_by_project_type(
                db, project_id, categories,
            )
        except Exception as _fe:
            logger.warning("split_library_category_filter_failed: %s", _fe)

    if not categories:
        logger.info("split_library_no_categories project=%s", project_id)
        return 0

    # 2. 중복 분할 방지
    existing_split = await db.fetchone(
        """SELECT COUNT(*) as cnt FROM nodes
           WHERE dag_id=? AND name LIKE '컴포넌트 라이브러리 (%'
           AND node_type='TASK' AND state != 'SKIPPED'""",
        (dag_id,),
    )
    if existing_split and existing_split["cnt"] > 0:
        logger.info("split_library_already_done dag=%s existing=%d", dag_id[:8], existing_split["cnt"])
        return 0

    # 3. 원본 노드 정보 로드
    original = await db.fetchone(
        "SELECT id, dag_id, assigned_model, qa_pair_node_id FROM nodes WHERE id=?",
        (node_id,),
    )
    if not original:
        logger.warning("split_library_no_original node=%s", node_id)
        return 0

    model = original["assigned_model"] or "sonnet"
    original_qa_id = original["qa_pair_node_id"]

    # 4. 업스트림 노드 찾기 (원본 TASK로 향하는 엣지의 from 노드들)
    upstream_rows = await db.fetchall(
        "SELECT from_node_id FROM edges WHERE to_node_id=? AND dag_id=? AND is_active=1",
        (node_id, dag_id),
    )
    upstream_ids = [r["from_node_id"] for r in upstream_rows] if upstream_rows else []

    # 5. 다운스트림 노드 찾기 (원본 QA에서 나가는 엣지의 to 노드들)
    downstream_ids = []
    if original_qa_id:
        downstream_rows = await db.fetchall(
            "SELECT to_node_id FROM edges WHERE from_node_id=? AND dag_id=? AND is_active=1",
            (original_qa_id, dag_id),
        )
        downstream_ids = [r["to_node_id"] for r in downstream_rows] if downstream_rows else []

    # GATE 노드 ID (fallback: phase='DESIGN' GATE)
    if not downstream_ids:
        gate_node = await db.fetchone(
            "SELECT id FROM nodes WHERE dag_id=? AND node_type='GATE' AND phase='DESIGN' LIMIT 1",
            (dag_id,),
        )
        if gate_node:
            downstream_ids = [gate_node["id"]]

    # 6. 원본 TASK + QA → SKIPPED
    await db.execute(
        "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
        (now, node_id),
    )
    if original_qa_id:
        await db.execute(
            "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
            (now, original_qa_id),
        )

    # 6-1. (NEW) umbrella 노드의 outgoing edges 비활성화
    # 다운스트림 (registry/recipe/assembly) 이 SKIPPED umbrella 에 의존하면
    # 영구 미충족 dep 로 BLOCKED 상태 영원히 유지됨.
    # 새 sub-task 들이 이미 다운스트림 edge 를 추가했으므로 umbrella edge 는 안전하게 비활성화.
    await db.execute(
        "UPDATE edges SET is_active=0 WHERE from_node_id=? AND dag_id=?",
        (node_id, dag_id),
    )
    if original_qa_id:
        await db.execute(
            "UPDATE edges SET is_active=0 WHERE from_node_id=? AND dag_id=?",
            (original_qa_id, dag_id),
        )

    # 7. 카테고리별 서브노드 생성
    created_count = 0
    for cat in categories:
        cat_name = cat["name"]
        cat_desc = cat.get("description", "")
        task_id = str(_uuid.uuid4())
        qa_id = str(_uuid.uuid4())
        node_name = f"컴포넌트 라이브러리 ({cat_name})"

        # 서브 TASK에 전달할 카테고리 지시 (자연어 — JSON 메타데이터가 아닌 명확한 지시)
        cat_instruction = (
            f"이 노드는 '{cat_name}' 카테고리의 컴포넌트만 생성합니다.\n"
            f"카테고리 설명: {cat_desc}\n"
            f"다른 카테고리의 컴포넌트는 생성하지 마세요.\n"
            f"반드시 순수 JSON 배열로만 출력하세요."
        )

        # TASK 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, qa_pair_node_id, description,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'TASK', 'DESIGN', ?, 'NOT_STARTED', ?, NULL, ?, ?, ?, 0)""",
            (task_id, dag_id, project_id, node_name, model,
             cat_instruction, now, now),
        )

        # QA 노드
        await db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, task_pair_node_id,
                created_at, updated_at, version)
               VALUES (?, ?, ?, 'QA', 'DESIGN', ?, 'NOT_STARTED', ?, ?, ?, ?, 0)""",
            (qa_id, dag_id, project_id, f"[QA] {node_name}", model,
             task_id, now, now),
        )

        # TASK ↔ QA 양방향
        await db.execute(
            "UPDATE nodes SET qa_pair_node_id=? WHERE id=?",
            (qa_id, task_id),
        )

        # 엣지: upstream → sub-TASK
        for up_id in upstream_ids:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'DEPENDS_ON', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, up_id, task_id, now),
            )

        # 엣지: TASK → QA
        await db.execute(
            """INSERT OR IGNORE INTO edges
               (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
               VALUES (?, ?, ?, ?, 'QA_PAIR', ?, 1)""",
            (str(_uuid.uuid4()), dag_id, task_id, qa_id, now),
        )

        # 엣지: QA → downstream (GATE 등)
        for down_id in downstream_ids:
            await db.execute(
                """INSERT OR IGNORE INTO edges
                   (id, dag_id, from_node_id, to_node_id, edge_type, created_at, is_active)
                   VALUES (?, ?, ?, ?, 'GATE_WAIT', ?, 1)""",
                (str(_uuid.uuid4()), dag_id, qa_id, down_id, now),
            )

        created_count += 1
        logger.info(
            "split_library_node_created category=%s task=%s",
            cat_name, task_id[:8],
        )

    # 8. DAG total_nodes 업데이트
    new_nodes = created_count * 2  # TASK + QA per category
    await db.execute(
        "UPDATE dags SET total_nodes = total_nodes + ? - 2, updated_at=? WHERE id=?",
        (new_nodes, now, dag_id),
    )

    logger.info(
        "split_library_complete project=%s categories=%d nodes=%d original_skipped=%s",
        project_id, created_count, new_nodes, node_id[:8],
    )

    return created_count
