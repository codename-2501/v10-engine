"""
engine/tools/verify_budget_scaling.py  (V10)

Phase 예산 스케일링 역시뮬레이션.
과거 engagement 의 intake raw_json → SizeProfile → 스케일링 결과를
실제 소비량(agent_token_usage)과 비교해 한도 적정성 검증.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from engine.db.adapter import create_adapter
from engine.intake.size_estimator import estimate_size
from engine.core.budget_scaler import scale_engagement_budget, BASE_BUDGET


async def main() -> int:
    db = create_adapter(os.environ.get("DATABASE_URL", "sqlite:///platform.db"))

    rows = await db.fetchall(
        """SELECT e.id, e.name, e.global_context
           FROM engagements e
           WHERE e.global_context IS NOT NULL AND e.global_context != ''
           LIMIT 20"""
    )

    print(f"=== Phase 예산 스케일링 역시뮬레이션 ({len(rows)} engagements) ===\n")

    pass_count = 0
    fail_count = 0
    for r in rows:
        eid = r["id"]
        try:
            raw = json.loads(r["global_context"])
        except Exception:
            print(f"  [SKIP] {eid[:8]} raw_json 파싱 실패")
            continue

        profile = estimate_size(raw)
        override = scale_engagement_budget(profile)

        # 실측 소비
        usage = await db.fetchall(
            """SELECT phase, SUM(input_tokens + output_tokens) AS used
               FROM agent_token_usage WHERE engagement_id=? GROUP BY phase""",
            (eid,),
        )
        used_map = {u["phase"]: u["used"] for u in usage}

        print(f"[{eid[:8]}] {r['name'][:40]} type={profile.project_type} screens={profile.screens_est}")
        print(f"  {'Phase':<10} {'Base':>10} {'V10':>10} {'Actual':>10} {'Fit':>8}")
        for phase in ("DEFINE", "DESIGN", "BUILD", "VERIFY", "DELIVER"):
            base = BASE_BUDGET[phase]
            v10 = override[phase]
            actual = used_map.get(phase, 0)
            if actual == 0:
                fit = "N/A"
            elif v10 >= actual:
                pct = actual / v10 * 100
                fit = f"✅ {pct:.0f}%"
                pass_count += 1
            else:
                shortage = actual - v10
                fit = f"❌ -{shortage//1000}K"
                fail_count += 1
            print(f"  {phase:<10} {base:>10,} {v10:>10,} {actual:>10,} {fit:>8}")
        print()

    print(f"=== 결과: PASS={pass_count} FAIL={fail_count} ===")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
