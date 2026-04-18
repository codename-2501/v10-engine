"""
Stage 16: Baseline vs After 측정 자동화 (D9).

사용법:
  python3 tools/benchmark_engagement.py baseline --engagement <id>
  python3 tools/benchmark_engagement.py after --engagement <id>
  python3 tools/benchmark_engagement.py compare --baseline <file> --after <file>

지표:
  - total_input_tokens / total_output_tokens (phase 별)
  - total_time_sec (DESIGN 시작 ~ GATE AWAITING_APPROVAL)
  - max_single_call_tokens
  - total_529_count (logs/server.out grep)
  - chunk_success_rate (atomic_state COMPLETE / (COMPLETE + FAILED))
  - placeholder_section_count (UI 시안 data-incomplete="true")
  - style_broken_section_count (>50% 클래스 정의 누락)
  - coverage_ratio (coverage_report 평균)

출력: JSON 파일 1개 per run. compare 는 before/after diff + 변화율 리포트.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def collect_metrics(db_path: str, engagement_id: str) -> dict:
    """engagement 전체 지표 수집."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    metrics: dict = {
        "engagement_id": engagement_id,
        "collected_at": _now_iso(),
    }

    # 1. Token usage (phase 별)
    rows = cur.execute(
        """SELECT phase,
                  COALESCE(SUM(input_tokens), 0) AS in_tok,
                  COALESCE(SUM(output_tokens), 0) AS out_tok,
                  COUNT(*) AS calls,
                  COALESCE(MAX(input_tokens), 0) AS max_in
           FROM agent_token_usage
           WHERE engagement_id=?
           GROUP BY phase""",
        (engagement_id,),
    ).fetchall()
    metrics["tokens_by_phase"] = [dict(r) for r in rows]
    metrics["total_input_tokens"] = sum(r["in_tok"] for r in rows)
    metrics["total_output_tokens"] = sum(r["out_tok"] for r in rows)
    metrics["total_calls"] = sum(r["calls"] for r in rows)
    metrics["max_single_call_input_tokens"] = max(
        (r["max_in"] for r in rows), default=0,
    )

    # 2. Time span (첫 노드 생성 ~ GATE AWAITING_APPROVAL)
    row = cur.execute(
        """SELECT MIN(created_at) AS start_at, MAX(updated_at) AS end_at
           FROM nodes
           WHERE project_id IN (SELECT id FROM projects WHERE engagement_id=?)
             AND phase='DESIGN'""",
        (engagement_id,),
    ).fetchone()
    metrics["design_start"] = row["start_at"] if row else None
    metrics["design_end"] = row["end_at"] if row else None

    # 3. Coverage
    rows = cur.execute(
        """SELECT AVG(CAST(produced_count AS REAL) / NULLIF(expected_count, 0)) AS avg_ratio,
                  SUM(CASE WHEN produced_count < expected_count THEN 1 ELSE 0 END) AS incomplete_nodes
           FROM coverage_report WHERE engagement_id=?""",
        (engagement_id,),
    ).fetchone()
    if rows:
        metrics["avg_coverage_ratio"] = rows["avg_ratio"]
        metrics["incomplete_nodes"] = rows["incomplete_nodes"]

    # 4. atomic_state 집계
    rows = cur.execute(
        """SELECT status, COUNT(*) AS cnt FROM atomic_state
           WHERE engagement_id=? GROUP BY status""",
        (engagement_id,),
    ).fetchall()
    metrics["atomic_state_summary"] = {r["status"]: r["cnt"] for r in rows}

    # 5. UI 시안 품질 (placeholder / style-broken)
    arts = cur.execute(
        """SELECT av.storage_path AS html
           FROM nodes n
           JOIN projects p ON p.id=n.project_id
           JOIN artifacts a ON a.node_id=n.id
           JOIN artifact_versions av
             ON av.artifact_id=a.id AND av.version_num=a.current_version
           WHERE p.engagement_id=? AND n.name LIKE '%UI%시안%'""",
        (engagement_id,),
    ).fetchall()
    placeholder_total = 0
    broken_total = 0
    for art in arts:
        html = art["html"] or ""
        placeholder_total += html.count('data-incomplete="true"')
        # 간이 style-broken: class 사용 > 정의 비율
        classes_used = set(re.findall(r'class=[\'"]([^\'"]*)[\'"]', html))
        used_flat: set[str] = set()
        for c in classes_used:
            used_flat.update(c.split())
        defined = set(
            re.findall(r'\.[a-z][a-z0-9_-]+(?=\s*[,\{])', html)
        )
        defined = {d.lstrip(".") for d in defined}
        broken = len(used_flat - defined)
        if len(used_flat) > 0 and broken / len(used_flat) > 0.5:
            broken_total += 1
    metrics["ui_placeholder_sections"] = placeholder_total
    metrics["ui_style_broken_artifacts"] = broken_total

    # 6. logs/server.out 에서 529 / transient_retry grep (최근)
    log_path = ROOT / "logs" / "server.out"
    if log_path.exists():
        try:
            content = log_path.read_text(errors="replace")
            metrics["total_529_in_log"] = len(
                re.findall(r"529|overloaded|transient_retry", content),
            )
        except Exception:
            metrics["total_529_in_log"] = None

    conn.close()
    return metrics


def compare(before: dict, after: dict) -> dict:
    """before/after 비교 리포트."""
    def pct(b, a):
        if not b: return None
        return round((a - b) / b * 100, 2)

    diff = {}
    for k in ("total_input_tokens", "total_output_tokens", "total_calls",
              "max_single_call_input_tokens", "ui_placeholder_sections",
              "ui_style_broken_artifacts", "total_529_in_log",
              "incomplete_nodes"):
        b = before.get(k)
        a = after.get(k)
        if isinstance(b, (int, float)) and isinstance(a, (int, float)):
            diff[k] = {"before": b, "after": a, "delta_pct": pct(b, a)}

    # coverage 는 ratio (높을수록 좋음)
    b = before.get("avg_coverage_ratio")
    a = after.get("avg_coverage_ratio")
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        diff["avg_coverage_ratio"] = {
            "before": b, "after": a, "delta_pp": round((a - b) * 100, 2),
        }

    return {
        "baseline": before,
        "after": after,
        "diff": diff,
        "compared_at": _now_iso(),
    }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("baseline", help="baseline 지표 수집")
    p_collect.add_argument("--engagement", required=True)
    p_collect.add_argument("--db", default=str(ROOT / "platform.db"))
    p_collect.add_argument("--out", default="benchmark_baseline.json")

    p_after = sub.add_parser("after", help="after 지표 수집")
    p_after.add_argument("--engagement", required=True)
    p_after.add_argument("--db", default=str(ROOT / "platform.db"))
    p_after.add_argument("--out", default="benchmark_after.json")

    p_cmp = sub.add_parser("compare", help="before vs after 비교")
    p_cmp.add_argument("--baseline", required=True)
    p_cmp.add_argument("--after", required=True)
    p_cmp.add_argument("--out", default="benchmark_report.json")

    args = parser.parse_args()

    if args.cmd in ("baseline", "after"):
        metrics = asyncio.run(collect_metrics(args.db, args.engagement))
        Path(args.out).write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[{args.cmd}] saved: {args.out}")
        print(f"  total_input={metrics.get('total_input_tokens'):,}, "
              f"total_output={metrics.get('total_output_tokens'):,}")
        print(f"  max_single_call={metrics.get('max_single_call_input_tokens'):,}")
        print(f"  coverage={metrics.get('avg_coverage_ratio')}")
        print(f"  placeholder={metrics.get('ui_placeholder_sections')}, "
              f"broken={metrics.get('ui_style_broken_artifacts')}")

    elif args.cmd == "compare":
        before = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        after = json.loads(Path(args.after).read_text(encoding="utf-8"))
        report = compare(before, after)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[compare] saved: {args.out}")
        for k, v in report["diff"].items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
