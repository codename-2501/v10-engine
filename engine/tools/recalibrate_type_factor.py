"""
engine/tools/recalibrate_type_factor.py  (V10-04)

실측 데이터(agent_token_usage) 기반으로 budget_type_factors 자동 재계산.

알고리즘:
  1. engagements.global_context → SizeProfile 재추출 → project_type 분류
  2. agent_token_usage 집계 → phase별 실측 token 합계
  3. 프로젝트 타입 × phase 그룹별 IQR 이상치 제거
  4. 중앙값 / BASE_BUDGET[phase] → 새 factor 계산
  5. sample_size >= MIN_SAMPLES 인 경우만 업데이트 (기본 3)
  6. budget_type_factors UPDATE + budget_factor_calibrations INSERT

사용:
  # dry-run (기본) — 변경 안 함
  PYTHONPATH=. python3 engine/tools/recalibrate_type_factor.py

  # 실제 적용
  PYTHONPATH=. python3 engine/tools/recalibrate_type_factor.py --apply

  # 최소 샘플 크기 조정
  PYTHONPATH=. python3 engine/tools/recalibrate_type_factor.py --min-samples 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from engine.db.adapter import create_adapter
from engine.intake.size_estimator import estimate_size
from engine.core.budget_scaler import BASE_BUDGET


MIN_FACTOR = 0.3     # 팩터 하한 (너무 작으면 의미 없음)
MAX_FACTOR = 5.0     # 팩터 상한 (과도 스케일 방지)
IQR_K = 1.5          # Tukey fence multiplier


def _remove_outliers_iqr(values: list[float]) -> list[float]:
    """IQR 기반 이상치 제거. 샘플 4개 미만이면 그대로 반환."""
    if len(values) < 4:
        return values
    q1, q3 = statistics.quantiles(values, n=4)[0], statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    lo = q1 - IQR_K * iqr
    hi = q3 + IQR_K * iqr
    return [v for v in values if lo <= v <= hi]


async def _collect_usage_by_type(db: Any) -> dict[tuple[str, str], list[int]]:
    """
    (project_type, phase) → [token_totals] 매핑.

    각 engagement 의 raw_json 을 estimate_size 로 재분류 → project_type 결정.
    """
    rows = await db.fetchall(
        """SELECT e.id, e.global_context,
                  u.phase, SUM(u.input_tokens + u.output_tokens) AS total
           FROM engagements e
           LEFT JOIN agent_token_usage u ON u.engagement_id = e.id
           WHERE e.global_context IS NOT NULL AND e.global_context != ''
             AND u.phase IS NOT NULL
           GROUP BY e.id, u.phase"""
    )
    buckets: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        try:
            raw = json.loads(r["global_context"])
        except Exception:
            continue
        profile = estimate_size(raw)
        key = (profile.project_type, r["phase"])
        buckets.setdefault(key, []).append(int(r["total"] or 0))
    return buckets


async def _fetch_current_factor(db: Any, project_type: str, phase: str) -> float | None:
    row = await db.fetchone(
        "SELECT factor FROM budget_type_factors WHERE project_type=? AND phase=?",
        (project_type, phase),
    )
    return float(row["factor"]) if row else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 DB 갱신 (기본: dry-run)")
    ap.add_argument("--min-samples", type=int, default=3,
                    help="새 factor 적용에 필요한 최소 샘플 수 (기본 3)")
    ap.add_argument("--triggered-by", default="cli", help="이력 기록용 트리거 소스")
    args = ap.parse_args()

    db = create_adapter(os.environ.get("DATABASE_URL", "sqlite:///platform.db"))

    print(f"=== V10 TYPE_FACTOR 재튜닝 ({'APPLY' if args.apply else 'DRY-RUN'}) ===\n")
    buckets = await _collect_usage_by_type(db)
    if not buckets:
        print("  agent_token_usage 데이터 없음 — 재튜닝 불가")
        return 0

    updates: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for (ptype, phase), values in sorted(buckets.items()):
        if phase not in BASE_BUDGET:
            continue
        sample_n = len(values)
        if sample_n < args.min_samples:
            print(f"  [SKIP] {ptype}/{phase} sample_size={sample_n} (< {args.min_samples})")
            continue

        # 이상치 제거
        clean = _remove_outliers_iqr([float(v) for v in values])
        if len(clean) < args.min_samples:
            print(f"  [SKIP] {ptype}/{phase} outlier 제거 후 {len(clean)} (< {args.min_samples})")
            continue

        median_actual = int(statistics.median(clean))
        base = BASE_BUDGET[phase]
        new_factor = round(median_actual / base, 3)
        new_factor = max(MIN_FACTOR, min(MAX_FACTOR, new_factor))

        old_factor = await _fetch_current_factor(db, ptype, phase)
        if old_factor is None:
            old_factor = 1.0

        diff = new_factor - old_factor
        arrow = "↑" if diff > 0.05 else ("↓" if diff < -0.05 else "=")
        print(
            f"  {ptype:<6}/{phase:<8} old={old_factor:.2f} → new={new_factor:.2f} "
            f"{arrow} median={median_actual:>10,} samples={sample_n} (clean={len(clean)})"
        )
        updates.append({
            "project_type": ptype, "phase": phase,
            "old_factor": old_factor, "new_factor": new_factor,
            "sample_size": len(clean), "median_actual": median_actual,
        })

    if not updates:
        print("\n  갱신할 항목 없음 (모두 샘플 부족 또는 동일값)")
        return 0

    if not args.apply:
        print(f"\n  [DRY-RUN] {len(updates)}건 갱신 예상. --apply 로 실제 반영.")
        return 0

    # 실제 DB 갱신
    applied = 0
    for u in updates:
        # UPDATE budget_type_factors
        await db.execute(
            """UPDATE budget_type_factors
               SET factor=?, sample_size=?, source='measured',
                   last_calibrated_at=?, note='recalibrate_type_factor'
               WHERE project_type=? AND phase=?""",
            (u["new_factor"], u["sample_size"], now, u["project_type"], u["phase"]),
        )
        # INSERT 이력
        await db.execute(
            """INSERT INTO budget_factor_calibrations
               (id, project_type, phase, old_factor, new_factor, sample_size,
                median_actual, base_budget, triggered_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), u["project_type"], u["phase"],
                u["old_factor"], u["new_factor"], u["sample_size"],
                u["median_actual"], BASE_BUDGET[u["phase"]],
                args.triggered_by, now,
            ),
        )
        applied += 1

    print(f"\n  ✓ {applied}건 UPDATE 완료. 다음 intake 변환 시 즉시 반영.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
