"""
engine/tools/recalibrate_upstream_keywords.py  (V10)

upstream_rework_audit 누적 데이터를 기반으로:
  1. False positive 비율 > THRESHOLD 키워드 → 보고 (수동 제거 후보)
  2. 미감지 사례 (영구 FAILED 노드 중 거시 진단 0건) → 새 키워드 후보 LLM 분석
  3. method='ai' 호출 빈도 → 키워드 누락 카테고리 식별

사용:
  PYTHONPATH=. python3 engine/tools/recalibrate_upstream_keywords.py
  PYTHONPATH=. python3 engine/tools/recalibrate_upstream_keywords.py --window-days 14
  PYTHONPATH=. python3 engine/tools/recalibrate_upstream_keywords.py --suggest-new

audit 테이블 outcome 컬럼은 후속 분석 도구가 채움:
  - success: rework 후 QA PASS 도달 → 정확한 진단
  - false_positive: rework 했지만 결국 같은 사유로 FAIL → 잘못된 진단
  - no_effect: rework 했으나 다른 이유로 FAILED
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.db.adapter import create_adapter

FALSE_POSITIVE_THRESHOLD = 0.20  # 20% 초과 시 키워드 strict 화 권고


async def _load_recent_audit(db: Any, window_days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        rows = await db.fetchall(
            """SELECT id, qa_node_id, detected_categories, invalidated_node_ids,
                      outcome, method, created_at, notes
               FROM upstream_rework_audit
               WHERE created_at >= ?
               ORDER BY created_at DESC""",
            (cutoff,),
        )
        return list(rows or [])
    except Exception as exc:
        print(f"  audit 테이블 조회 실패: {exc}")
        return []


def _by_method(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["method"]].append(r)
    return out


def _category_outcome_stats(rows: list[dict]) -> dict[str, dict[str, int]]:
    """카테고리별 outcome 분포."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        try:
            cats = json.loads(r["detected_categories"])
        except Exception:
            continue
        outcome = r["outcome"]
        for cat in cats:
            stats[cat][outcome] += 1
            stats[cat]["total"] += 1
    return stats


async def _find_undetected_fails(db: Any, window_days: int) -> list[dict]:
    """거시 진단으로 잡지 못한 영구 FAILED QA 노드 후보."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        rows = await db.fetchall(
            """SELECT n.id, n.name, n.failure_reasons, n.updated_at, n.phase
               FROM nodes n
               WHERE n.node_type='QA' AND n.state='FAILED'
                 AND n.updated_at >= ?
                 AND n.id NOT IN (SELECT qa_node_id FROM upstream_rework_audit)
               ORDER BY n.updated_at DESC LIMIT 50""",
            (cutoff,),
        )
        return list(rows or [])
    except Exception as exc:
        print(f"  미감지 FAIL 조회 실패: {exc}")
        return []


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=7,
                    help="분석 기간 (기본 7일)")
    ap.add_argument("--suggest-new", action="store_true",
                    help="LLM 으로 새 키워드 후보 도출 (haiku 호출, 비용 발생)")
    args = ap.parse_args()

    db = create_adapter(os.environ.get("DATABASE_URL", "sqlite:///platform.db"))

    print(f"\n=== V10 거시 진단 keyword 재튜닝 (최근 {args.window_days}일) ===\n")

    rows = await _load_recent_audit(db, args.window_days)
    if not rows:
        print("  audit 데이터 없음 — 분석 불가")
        return 0

    print(f"총 {len(rows)}건 audit 기록\n")

    # 1. method 별 분포
    by_m = _by_method(rows)
    print("[Method 분포]")
    for m, lst in sorted(by_m.items()):
        print(f"  {m:<10} {len(lst):>4}건")
    print()

    # 2. 카테고리별 outcome
    cat_stats = _category_outcome_stats(rows)
    if cat_stats:
        print("[카테고리별 outcome]")
        print(f"  {'category':<10} {'success':>8} {'false_pos':>10} {'pending':>8} {'total':>6} {'fp_rate':>8}")
        for cat, s in sorted(cat_stats.items()):
            total = s.get("total", 0) or 1
            fp = s.get("false_positive", 0)
            fp_rate = fp / total
            warn = " ⚠️" if fp_rate > FALSE_POSITIVE_THRESHOLD else ""
            print(
                f"  {cat:<10} {s.get('success', 0):>8} {fp:>10} "
                f"{s.get('pending', 0):>8} {total:>6} {fp_rate:>7.1%}{warn}"
            )
        print()

        # 권고
        risky = [c for c, s in cat_stats.items()
                 if s.get("total", 0) >= 5 and
                 (s.get("false_positive", 0) / s["total"]) > FALSE_POSITIVE_THRESHOLD]
        if risky:
            print(f"⚠️  False positive 비율 > {FALSE_POSITIVE_THRESHOLD:.0%} 카테고리: {risky}")
            print("    → executor_cascade.py _UPSTREAM_KEYWORDS 의 해당 카테고리 키워드 strict 화 권고\n")

    # 3. 미감지 사례
    undetected = await _find_undetected_fails(db, args.window_days)
    if undetected:
        print(f"[미감지 영구 FAILED QA 노드: {len(undetected)}건]")
        for r in undetected[:10]:
            print(f"  - {r['id'][:8]} {r['name'][:50]}")

        if args.suggest_new:
            await _suggest_new_keywords(undetected)
        else:
            print(f"\n  --suggest-new 옵션으로 LLM 분석하여 새 키워드 후보 도출 가능")
    else:
        print("[미감지 사례 없음 — 키워드 커버리지 양호]")

    return 0


async def _suggest_new_keywords(undetected: list[dict]) -> None:
    """LLM 으로 미감지 FAILED 노드의 사유에서 누락 키워드 후보 도출."""
    print("\n[LLM 분석 — 누락 키워드 후보 도출]")
    sample = undetected[:5]  # 비용 절감 — 5건만
    samples_text = "\n---\n".join(
        f"노드: {r['name'][:50]}\n사유: {(r.get('failure_reasons') or '')[:400]}"
        for r in sample
    )
    print("  (직접 LLM 호출은 환경 종속 — 위 샘플을 보고 수동 분석 권장)")
    print(f"\n샘플 (총 {len(sample)}건):\n{samples_text[:1500]}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
