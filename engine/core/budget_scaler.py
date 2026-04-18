"""
engine/core/budget_scaler.py  (V10)

Phase 토큰 예산 동적 스케일링 (Level 1: Intake-time Pre-scale).

입력: SizeProfile (from size_estimator)
출력: dict[phase, phase_limit] — engagements.phase_budget_override 에 저장

실측 데이터 기반 휴리스틱 (V9 agent_token_usage 집계):
  DEFINE  평균 357K / 한도 600K (여유)
  DESIGN  평균 1,517K / 한도 900K → Habit Tracker 같은 앱에서 초과
  BUILD   평균 3,440K / 한도 1.2M → 심각한 괴리
  VERIFY  평균 1,161K / 한도 500K → 2배 부족
  DELIVER 평균 80K / 한도 500K → 과다 배정

→ 프로젝트 타입별 base × TYPE_FACTOR × size_factor.

참고: budget_enforcer.py (코어 5파일) 는 수정하지 않음.
      기존 get_budget() 이 engagements.phase_budget_override 를 읽는 경로만 추가됨
      (coexistence via executor.py 가 override 를 budget_spec 에 merge).
"""
from __future__ import annotations

import logging
from typing import Any

from engine.intake.size_estimator import SizeProfile

logger = logging.getLogger(__name__)


# 코어 budget_enforcer.TOKEN_BUDGET["phase_limit"] 와 동일 (복제 — 코어 불변)
BASE_BUDGET: dict[str, int] = {
    "DEFINE":    600_000,
    "DESIGN":    900_000,
    "BUILD":   1_200_000,
    "VERIFY":    500_000,
    "DELIVER":   500_000,
}

# 프로젝트 타입별 Phase 가중치 (실측 데이터 기반)
TYPE_FACTOR: dict[str, dict[str, float]] = {
    "app": {
        "DEFINE": 0.8, "DESIGN": 1.8, "BUILD": 1.5,
        "VERIFY": 1.2, "DELIVER": 0.4,
    },
    "si": {
        "DEFINE": 1.3, "DESIGN": 1.2, "BUILD": 2.5,
        "VERIFY": 1.8, "DELIVER": 0.6,
    },
    "mlops": {
        "DEFINE": 1.0, "DESIGN": 1.5, "BUILD": 2.8,
        "VERIFY": 2.0, "DELIVER": 0.5,
    },
    "data": {
        "DEFINE": 1.0, "DESIGN": 1.0, "BUILD": 2.0,
        "VERIFY": 1.3, "DELIVER": 0.4,
    },
    "mixed": {
        "DEFINE": 1.1, "DESIGN": 1.4, "BUILD": 1.8,
        "VERIFY": 1.4, "DELIVER": 0.5,
    },
}

MIN_SIZE_FACTOR = 0.7
MAX_SIZE_FACTOR = 2.5

# Level 2 Runtime Realloc 한계
MAX_REALLOC_PER_ENGAGEMENT = 2


def _size_factor(profile: SizeProfile) -> float:
    """
    규모 지표 → 단일 스케일 팩터.

    10 화면 기본, 1 화면당 +5% / 1 feature당 +3% / 1 integration당 +2%.
    """
    raw = 1.0
    raw += (profile.screens_est - 10) * 0.05
    raw += (profile.features - 5) * 0.03
    raw += profile.integrations * 0.02
    return max(MIN_SIZE_FACTOR, min(MAX_SIZE_FACTOR, raw))


def scale_engagement_budget(
    profile: SizeProfile,
    factors_override: dict[str, float] | None = None,
) -> dict[str, int]:
    """
    SizeProfile → phase 예산 override dict (저장용).

    우선순위:
      1. factors_override (호출자가 DB 조회 결과 전달)
      2. TYPE_FACTOR 하드코딩 seed (fallback)

    저장 예: {"DEFINE": 658000, "DESIGN": 2220000, ...}
    """
    if factors_override is not None:
        factors = factors_override
    else:
        factors = TYPE_FACTOR.get(profile.project_type, TYPE_FACTOR["mixed"])
    sf = _size_factor(profile)

    result: dict[str, int] = {}
    for phase, base in BASE_BUDGET.items():
        multiplier = factors.get(phase, 1.0) * sf
        result[phase] = int(base * multiplier)

    logger.info(
        "budget_scaler_calculated type=%s size_factor=%.2f result=%s",
        profile.project_type, sf,
        {p: f"{v/1000:.0f}K" for p, v in result.items()},
    )
    # Prometheus gauge 방출 (noop fallback 안전)
    try:
        from engine.observability.metrics import V10_BUDGET_MULTIPLIER
        for phase, limit in result.items():
            multiplier = limit / max(1, BASE_BUDGET.get(phase, 1))
            V10_BUDGET_MULTIPLIER.labels(
                phase=phase, project_type=profile.project_type,
            ).set(round(multiplier, 3))
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# DB 조회: budget_type_factors 테이블 (V10 마이그레이션 038)
# ---------------------------------------------------------------------------

async def load_type_factors(db: Any, project_type: str) -> dict[str, float]:
    """
    budget_type_factors 테이블에서 project_type 에 맞는 factor dict 조회.
    테이블 없거나 항목 없으면 TYPE_FACTOR hardcoded 복귀.
    """
    try:
        rows = await db.fetchall(
            "SELECT phase, factor FROM budget_type_factors WHERE project_type=?",
            (project_type,),
        )
        if rows:
            return {r["phase"]: r["factor"] for r in rows}
    except Exception as exc:
        logger.debug("load_type_factors_fallback type=%s: %s", project_type, exc)
    return dict(TYPE_FACTOR.get(project_type, TYPE_FACTOR["mixed"]))


async def scale_engagement_budget_db(
    db: Any, profile: SizeProfile,
) -> dict[str, int]:
    """DB 조회 우선 + fallback. intake processor 에서 직접 사용."""
    factors = await load_type_factors(db, profile.project_type)
    return scale_engagement_budget(profile, factors_override=factors)


# ---------------------------------------------------------------------------
# DB 유틸 (engagements.phase_budget_override 읽기/쓰기)
# ---------------------------------------------------------------------------

async def save_override(db: Any, engagement_id: str, override: dict[str, int]) -> None:
    """engagement_id 의 phase_budget_override 컬럼 업데이트."""
    import json as _json
    await db.execute(
        "UPDATE engagements SET phase_budget_override=?, updated_at=datetime('now') WHERE id=?",
        (_json.dumps(override), engagement_id),
    )


async def load_override(db: Any, engagement_id: str) -> dict[str, int]:
    """engagement_id 의 phase_budget_override 조회. 없으면 BASE_BUDGET."""
    import json as _json
    row = await db.fetchone(
        "SELECT phase_budget_override FROM engagements WHERE id=?",
        (engagement_id,),
    )
    if row and row["phase_budget_override"]:
        try:
            return _json.loads(row["phase_budget_override"])
        except (ValueError, TypeError):
            pass
    return dict(BASE_BUDGET)


async def get_phase_limit(db: Any, engagement_id: str, phase: str) -> int:
    """engagement + phase 의 현재 한도 반환. override > BASE_BUDGET."""
    override = await load_override(db, engagement_id)
    return override.get(phase, BASE_BUDGET.get(phase, 1_000_000))


# ---------------------------------------------------------------------------
# Level 2 Runtime Realloc
# ---------------------------------------------------------------------------

async def try_budget_realloc(
    db: Any,
    engagement_id: str,
    need_phase: str,
    shortage: int,
) -> bool:
    """
    PhaseBudgetExceededError 발생 시 다른 Phase 여유 → 부족 Phase 로 이전.

    성공 시 True (engagements.phase_budget_override 갱신 + budget_realloc_log INSERT).
    실패 시 False (여유 없음 또는 realloc 한도 초과).
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    # 1. 재시도 한도 체크
    count_row = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM budget_realloc_log WHERE engagement_id=?",
        (engagement_id,),
    )
    already = count_row["cnt"] if count_row else 0
    if already >= MAX_REALLOC_PER_ENGAGEMENT:
        logger.info(
            "budget_realloc_max_reached engagement=%s count=%d",
            engagement_id[:8], already,
        )
        return False

    # 2. 현재 Phase별 사용량
    usage_rows = await db.fetchall(
        """SELECT phase, SUM(input_tokens + output_tokens) AS used
           FROM agent_token_usage WHERE engagement_id=? GROUP BY phase""",
        (engagement_id,),
    )
    usage = {r["phase"]: r["used"] for r in usage_rows}

    # 3. 현재 override (기본값 복사본으로 시작)
    override = await load_override(db, engagement_id)

    # 4. 여유 Phase 찾기 (뒷 Phase 우선: DELIVER → VERIFY → DEFINE 순)
    donor = None
    donor_available = 0
    for phase in ("DELIVER", "VERIFY", "DEFINE"):
        if phase == need_phase:
            continue
        limit = override.get(phase, BASE_BUDGET[phase])
        used = usage.get(phase, 0)
        available = limit - used
        if available > shortage * 1.2:  # 20% buffer
            if available > donor_available:
                donor = phase
                donor_available = available

    if donor is None:
        logger.info(
            "budget_realloc_no_donor engagement=%s need=%s shortage=%d",
            engagement_id[:8], need_phase, shortage,
        )
        return False

    # 5. 이전 실행
    transfer = int(shortage * 1.5)  # 1.5x 여유로 재시도 성공률 up
    override[donor] = override.get(donor, BASE_BUDGET[donor]) - transfer
    override[need_phase] = override.get(need_phase, BASE_BUDGET[need_phase]) + transfer

    await save_override(db, engagement_id, override)

    # 6. 이력 기록
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO budget_realloc_log
           (id, engagement_id, from_phase, to_phase, transferred, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(_uuid.uuid4()), engagement_id, donor, need_phase,
            transfer, f"PhaseBudgetExceeded on {need_phase}", now,
        ),
    )

    logger.info(
        "budget_realloc engagement=%s from=%s to=%s amount=%d (attempt %d/%d)",
        engagement_id[:8], donor, need_phase, transfer,
        already + 1, MAX_REALLOC_PER_ENGAGEMENT,
    )
    # Prometheus counter
    try:
        from engine.observability.metrics import V10_BUDGET_REALLOC_TOTAL
        V10_BUDGET_REALLOC_TOTAL.labels(
            from_phase=donor, to_phase=need_phase,
        ).inc()
    except Exception:
        pass
    return True
