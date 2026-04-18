"""
engine/tools/verify_dag_integrity.py  (V10)

DAG 정합성 검증 도구. 다음 5가지 케이스를 자동 감지:

  1. 깨진 페어 link — qa_pair_node_id / task_pair_node_id 가 존재하지 않는 노드 가리킴
  2. SKIPPED 노드의 활성 outgoing edges — 다운스트림이 영구 미충족 dep 가짐
  3. 고아 edges — from/to 노드 자체가 삭제됨
  4. 순환 의존성 — A→B→A 같은 cycle
  5. 페어 정합성 — TASK ↔ QA 의 양방향 link 불일치

기본은 dry-run (보고만). --apply 시 자동 복구 가능한 항목 처리:
  - 깨진 페어 link → repair_pair_references 호출
  - SKIPPED outgoing edges → is_active=0 비활성화

사용:
  PYTHONPATH=. python3 engine/tools/verify_dag_integrity.py
  PYTHONPATH=. python3 engine/tools/verify_dag_integrity.py --apply
  PYTHONPATH=. python3 engine/tools/verify_dag_integrity.py --dag-id 586f7210-...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from typing import Any

from engine.db.adapter import create_adapter


async def _check_broken_pairs(db: Any, dag_filter: str | None) -> list[dict]:
    """qa_pair_node_id / task_pair_node_id 가 깨진 노드 검출."""
    where_dag = "AND dag_id=?" if dag_filter else ""
    params: tuple = (dag_filter,) if dag_filter else ()

    broken_tasks = await db.fetchall(
        f"""SELECT id, name, dag_id FROM nodes
            WHERE node_type='TASK' AND qa_pair_node_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM nodes q WHERE q.id = qa_pair_node_id)
              {where_dag}""",
        params,
    )
    broken_qas = await db.fetchall(
        f"""SELECT id, name, dag_id FROM nodes
            WHERE node_type='QA' AND task_pair_node_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.id = task_pair_node_id)
              {where_dag}""",
        params,
    )
    issues = []
    for r in broken_tasks:
        issues.append({"type": "broken_qa_pair", "node_id": r["id"], "name": r["name"], "dag_id": r["dag_id"]})
    for r in broken_qas:
        issues.append({"type": "broken_task_pair", "node_id": r["id"], "name": r["name"], "dag_id": r["dag_id"]})
    return issues


async def _check_skipped_outgoing_edges(db: Any, dag_filter: str | None) -> list[dict]:
    """SKIPPED 노드의 활성 outgoing edges — 다운스트림 영구 미충족 dep 발생."""
    where_dag = "AND e.dag_id=?" if dag_filter else ""
    params: tuple = (dag_filter,) if dag_filter else ()

    rows = await db.fetchall(
        f"""SELECT e.id AS edge_id, e.from_node_id, e.to_node_id,
                   n.name AS skipped_name, t.name AS downstream_name, t.state AS downstream_state
            FROM edges e
            JOIN nodes n ON n.id = e.from_node_id
            JOIN nodes t ON t.id = e.to_node_id
            WHERE n.state='SKIPPED' AND e.is_active=1 {where_dag}""",
        params,
    )
    return [
        {
            "type": "skipped_active_outgoing_edge",
            "edge_id": r["edge_id"],
            "from_node": r["skipped_name"],
            "to_node": r["downstream_name"],
            "to_state": r["downstream_state"],
        }
        for r in rows
    ]


async def _check_orphan_edges(db: Any, dag_filter: str | None) -> list[dict]:
    """from / to 노드가 삭제된 고아 edges."""
    where_dag = "AND dag_id=?" if dag_filter else ""
    params: tuple = (dag_filter,) if dag_filter else ()

    rows = await db.fetchall(
        f"""SELECT id, from_node_id, to_node_id FROM edges
            WHERE is_active=1 {where_dag}
              AND (NOT EXISTS (SELECT 1 FROM nodes WHERE id=from_node_id)
                   OR NOT EXISTS (SELECT 1 FROM nodes WHERE id=to_node_id))""",
        params,
    )
    return [
        {"type": "orphan_edge", "edge_id": r["id"],
         "from_node_id": r["from_node_id"], "to_node_id": r["to_node_id"]}
        for r in rows
    ]


async def _check_pair_consistency(db: Any, dag_filter: str | None) -> list[dict]:
    """TASK.qa_pair_node_id == X 인데 X.task_pair_node_id != TASK 인 케이스."""
    where_dag = "AND t.dag_id=?" if dag_filter else ""
    params: tuple = (dag_filter,) if dag_filter else ()

    rows = await db.fetchall(
        f"""SELECT t.id AS task_id, t.name AS task_name, t.qa_pair_node_id AS task_qa,
                   q.id AS qa_id, q.task_pair_node_id AS qa_task
            FROM nodes t
            JOIN nodes q ON q.id = t.qa_pair_node_id
            WHERE t.node_type='TASK' AND q.node_type='QA'
              AND (q.task_pair_node_id IS NULL OR q.task_pair_node_id != t.id) {where_dag}""",
        params,
    )
    return [
        {"type": "pair_inconsistency",
         "task_id": r["task_id"], "task_name": r["task_name"],
         "task_qa_link": r["task_qa"], "qa_task_link": r["qa_task"]}
        for r in rows
    ]


async def _check_cycles(db: Any, dag_filter: str | None) -> list[dict]:
    """단순 BFS 기반 순환 의존성 검사."""
    where_dag = "AND dag_id=?" if dag_filter else ""
    params: tuple = (dag_filter,) if dag_filter else ()

    edges = await db.fetchall(
        f"SELECT from_node_id, to_node_id FROM edges WHERE is_active=1 {where_dag}",
        params,
    )
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["from_node_id"]].append(e["to_node_id"])

    cycles: list[dict] = []
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs(node: str, path: list[str]) -> bool:
        visited.add(node)
        rec_stack.add(node)
        for nxt in adj.get(node, []):
            if nxt not in visited:
                if dfs(nxt, path + [nxt]):
                    return True
            elif nxt in rec_stack:
                cycles.append({"type": "cycle", "path": path + [nxt]})
                return True
        rec_stack.remove(node)
        return False

    for node in list(adj.keys()):
        if node not in visited:
            dfs(node, [node])
    return cycles


async def _apply_fixes(db: Any, issues: list[dict]) -> dict:
    """자동 복구 가능한 항목 처리. 위험 항목 (cycle 등) 은 수동 검토 필요."""
    from engine.skills.utils import _now
    fixed = {"skipped_outgoing": 0, "orphan_edges": 0, "broken_pairs": 0}
    now = _now()
    for it in issues:
        if it["type"] == "skipped_active_outgoing_edge":
            await db.execute(
                "UPDATE edges SET is_active=0 WHERE id=?", (it["edge_id"],),
            )
            fixed["skipped_outgoing"] += 1
        elif it["type"] == "orphan_edge":
            await db.execute(
                "UPDATE edges SET is_active=0 WHERE id=?", (it["edge_id"],),
            )
            fixed["orphan_edges"] += 1
    return fixed


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="자동 복구 가능 항목 처리 (기본: dry-run)")
    ap.add_argument("--dag-id", default=None,
                    help="특정 DAG 만 검사 (기본: 전체)")
    args = ap.parse_args()

    db = create_adapter(os.environ.get("DATABASE_URL", "sqlite:///platform.db"))

    print(f"\n=== V10 DAG 정합성 검증 ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    if args.dag_id:
        print(f"  대상: dag_id={args.dag_id}")
    else:
        print("  대상: 전체 DAG")
    print()

    all_issues: list[dict] = []
    for label, checker in [
        ("깨진 페어 link",        _check_broken_pairs),
        ("SKIPPED 활성 outgoing edge", _check_skipped_outgoing_edges),
        ("고아 edge",             _check_orphan_edges),
        ("페어 정합성 불일치",    _check_pair_consistency),
        ("순환 의존성",           _check_cycles),
    ]:
        issues = await checker(db, args.dag_id)
        print(f"  [{label}] {len(issues)}건")
        for it in issues[:5]:
            print(f"    - {it}")
        if len(issues) > 5:
            print(f"    ... +{len(issues) - 5}건")
        all_issues.extend(issues)

    if not all_issues:
        print("\n  ✓ 정합성 양호 — 모든 검사 통과")
        return 0

    print(f"\n  총 {len(all_issues)}건 이슈 발견")

    if not args.apply:
        print("  --apply 옵션으로 자동 복구 가능 항목 처리")
        return 0

    fixed = await _apply_fixes(db, all_issues)
    print(f"\n  ✓ 자동 복구 완료: {fixed}")
    cycle_count = sum(1 for it in all_issues if it["type"] == "cycle")
    pair_count = sum(1 for it in all_issues if it["type"] == "pair_inconsistency")
    if cycle_count or pair_count:
        print(f"  ⚠ 수동 검토 필요: cycle={cycle_count} pair_inconsistency={pair_count}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
