"""
api/routes/habits.py
Personal Habit Tracker 라우터.

엔드포인트:
- POST /habits — 습관 생성
- GET /habits — 습관 목록 (+ 진행률)
- POST /habits/{id}/log — 오늘 실행 기록
- GET /habits/{id}/analysis — AI 분석 + 조언 (Phase F 활용: Vector + Gotchas)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["habits"])

# ─── 설정 상수 (환경변수로 오버라이드 가능) ───────────────────
# Vector search 최소 유사도. TFIDFHash fallback 기준 유사 텍스트가 ~0.7,
# 무관한 텍스트가 ~0.27이므로 0.5가 적절한 중간값.
import os as _os
HABIT_VECTOR_MIN_SIMILARITY = float(
    _os.environ.get("V9_HABIT_VECTOR_MIN_SIMILARITY", "0.5")
)
HABIT_VECTOR_TOP_K = int(_os.environ.get("V9_HABIT_VECTOR_TOP_K", "3"))
HABIT_RECENT_FAILURE_DAYS = int(_os.environ.get("V9_HABIT_FAILURE_DAYS", "3"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db() -> DatabaseAdapter:
    """의존성 주입 — 추후 app state에서 제공받음."""
    from api.server import state
    return state.db


def _get_episode_store() -> Any:
    """EpisodeStore 의존성."""
    from api.server import state
    return getattr(state, "episode_store", None)


# ───────────────────────────────────────────────────────────────
# POST /habits — 습관 생성
# ───────────────────────────────────────────────────────────────


@router.post(
    "/habits",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
    summary="습관 생성",
)
async def create_habit(
    payload: dict,
    db: DatabaseAdapter = Depends(_get_db),
) -> dict:
    """
    습관 생성.

    요청:
    {
      "user_id": "user123",
      "name": "아침 운동",
      "category": "health",
      "target_days": 3
    }

    응답:
    {
      "id": "habit-uuid",
      "user_id": "user123",
      "name": "아침 운동",
      "category": "health",
      "target_days": 3,
      "created_at": "2026-04-18T..."
    }
    """
    user_id = payload.get("user_id", "default_user")
    name = payload.get("name", "").strip()
    category = payload.get("category", "other")
    target_days = payload.get("target_days", 3)

    if not name:
        raise HTTPException(status_code=400, detail="name required")

    if category not in ["health", "learning", "productivity", "wellness", "other"]:
        category = "other"

    habit_id = str(uuid.uuid4())
    now = _now()

    try:
        await db.execute(
            """INSERT INTO habits (id, user_id, name, category, target_days, created_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (habit_id, user_id, name, category, target_days, now, "{}"),
        )
    except Exception as e:
        logger.error("habit_creation_failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create habit")

    return {
        "id": habit_id,
        "user_id": user_id,
        "name": name,
        "category": category,
        "target_days": target_days,
        "created_at": now,
    }


# ───────────────────────────────────────────────────────────────
# GET /habits — 습관 목록 + 진행률
# ───────────────────────────────────────────────────────────────


@router.get("/habits", response_model=dict, summary="습관 목록")
async def list_habits(
    user_id: str = "default_user",
    db: DatabaseAdapter = Depends(_get_db),
) -> dict:
    """
    사용자의 습관 목록 조회.

    쿼리 파라미터:
    - user_id: 사용자 ID (기본값: default_user)

    응답:
    {
      "habits": [
        {
          "id": "habit-uuid",
          "name": "아침 운동",
          "category": "health",
          "target_days": 3,
          "created_at": "2026-04-18T...",
          "streak": 5,
          "completion_rate": 0.83
        }
      ]
    }
    """
    try:
        rows = await db.fetchall(
            """SELECT id, name, category, target_days, created_at
               FROM habits
               WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id,),
        )
    except Exception as e:
        logger.error("habit_list_failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch habits")

    habits_list = []
    for row in rows:
        habit_dict = dict(row)
        habit_id = habit_dict["id"]

        # 이달 완료율 계산
        completion = await _get_completion_rate(db, habit_id)

        habits_list.append({
            **habit_dict,
            "streak": completion.get("streak", 0),
            "completion_rate": completion.get("rate", 0.0),
        })

    return {"habits": habits_list}


# ───────────────────────────────────────────────────────────────
# POST /habits/{habit_id}/log — 오늘 실행 기록
# ───────────────────────────────────────────────────────────────


@router.post(
    "/habits/{habit_id}/log",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
    summary="습관 실행 기록",
)
async def log_habit_execution(
    habit_id: str,
    payload: dict,
    db: DatabaseAdapter = Depends(_get_db),
    episode_store: Any = Depends(_get_episode_store),
) -> dict:
    """
    습관 실행을 오늘 기록.

    요청:
    {
      "user_id": "user123",
      "notes": "15분만 했음"
    }

    응답:
    {
      "log_id": "log-uuid",
      "habit_id": "habit-uuid",
      "logged_at": "2026-04-18T...",
      "notes": "15분만 했음"
    }

    Phase F: Gotchas 기록
    - 만약 같은 습관이 자주 실패되었다면, 이번 성공을 episodes에 저장
    - Vector search로 "같은 습관 실패 패턴" 검색 후 힌트 제시
    """
    user_id = payload.get("user_id", "default_user")
    notes = payload.get("notes", "")

    # 습관 존재 확인
    habit_row = await db.fetchone(
        "SELECT name FROM habits WHERE id = ?", (habit_id,)
    )
    if not habit_row:
        raise HTTPException(status_code=404, detail="Habit not found")

    habit_name = habit_row["name"]

    log_id = str(uuid.uuid4())
    now = _now()
    today = now.split("T")[0]

    try:
        await db.execute(
            """INSERT INTO habit_logs (id, habit_id, user_id, logged_at, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (log_id, habit_id, user_id, today, notes, now),
        )

        # Phase F: 성공 에피소드 기록 (백그라운드)
        if episode_store:
            import asyncio
            asyncio.create_task(
                episode_store.save_episode(
                    project_id=user_id,
                    node_id=habit_id,
                    node_name=habit_name,
                    episode_type="success",
                    content=f"습관 성공: {habit_name}. {notes}",
                    metadata={"habit_id": habit_id},
                )
            )

    except Exception as e:
        # 034 마이그레이션 이후: UNIQUE(habit_id, logged_at) 위반 시 409
        msg = str(e).lower()
        if "unique" in msg or "constraint" in msg:
            raise HTTPException(status_code=409, detail="이미 오늘 기록됨")
        logger.error("habit_log_failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to log execution")

    return {
        "log_id": log_id,
        "habit_id": habit_id,
        "logged_at": today,
        "notes": notes,
    }


# ───────────────────────────────────────────────────────────────
# GET /habits/{habit_id}/analysis — AI 분석 + 조언
# ───────────────────────────────────────────────────────────────


@router.get("/habits/{habit_id}/analysis", response_model=dict, summary="AI 분석 및 조언")
async def analyze_habit(
    habit_id: str,
    user_id: str = "default_user",
    db: DatabaseAdapter = Depends(_get_db),
    episode_store: Any = Depends(_get_episode_store),
) -> dict:
    """
    습관의 AI 분석 + 조언.

    응답:
    {
      "habit_name": "아침 운동",
      "completion_rate": 0.67,
      "patterns": {
        "best_day": "토요일 (80%)",
        "worst_day": "월요일 (30%)",
      },
      "insights": [
        "월요일 새벽 운동이 자주 실패합니다. 저녁으로 옮겨보세요.",
        "토요일 성공률이 높으니 주중에 토요일 공식 활용해보세요.",
      ],
      "warnings": [
        "최근 3일 연속 실패 감지됨 (Gotchas)"
      ]
    }

    Phase F 활용:
    - Vector Search: 유사 습관 성공 패턴 검색
    - Gotchas: 반복 실패 패턴 감지 (예: "월요일 새벽 운동 실패율 높음")
    """
    habit_row = await db.fetchone(
        "SELECT name, category, target_days FROM habits WHERE id = ?", (habit_id,)
    )
    if not habit_row:
        raise HTTPException(status_code=404, detail="Habit not found")

    habit_name = habit_row["name"]

    # 1. 기본 통계
    completion = await _get_completion_rate(db, habit_id)
    patterns = await _analyze_patterns(db, habit_id)

    # 2. Phase F: Vector Search (유사 습관 성공 패턴)
    insights = [
        "정기적인 실행을 유지하세요. 습관은 반복에서 형성됩니다.",
    ]

    if episode_store:
        try:
            # 성공 패턴 우선 검색 (본인 과거 success 에피소드)
            similar_success = await episode_store.search_similar_episodes(
                query=habit_name,
                project_id=user_id,
                episode_type="success",
                top_k=HABIT_VECTOR_TOP_K,
                min_similarity=HABIT_VECTOR_MIN_SIMILARITY,
            )
            if similar_success:
                sample = similar_success[0].get("content", "")[:150]
                insights.append(f"유사 성공 사례: {sample}")
        except Exception as e:
            logger.debug("vector_search_failed habit=%s: %s", habit_id[:8], e)

    # 3. Phase F: Gotchas (반복 실패 감지)
    warnings = []
    failure_count = await _count_recent_failures(
        db, habit_id, days=HABIT_RECENT_FAILURE_DAYS
    )
    if failure_count >= HABIT_RECENT_FAILURE_DAYS:
        warnings.append(
            f"최근 {failure_count}일 연속 실패 감지됨. 습관 실행 계획을 재검토해보세요."
        )

    # 패턴 기반 조언 — 실 데이터 있을 때만 의미 있음
    worst = patterns.get("worst_day", "")
    if worst and not any(
        marker in worst for marker in ("데이터 부족", "편차 없음", "분석 실패")
    ):
        insights.append(f"'{worst}' 달성이 어렵네요. 시간대를 바꿔보거나 목표를 낮춰보세요.")

    return {
        "habit_name": habit_name,
        "completion_rate": completion.get("rate", 0.0),
        "streak": completion.get("streak", 0),
        "patterns": patterns,
        "insights": insights,
        "warnings": warnings,
    }


# ───────────────────────────────────────────────────────────────
# 내부 유틸리티
# ───────────────────────────────────────────────────────────────


async def _get_completion_rate(db: DatabaseAdapter, habit_id: str) -> dict:
    """이달 완료율 + 연속 달성일(streak) 계산. 분모는 오늘까지 경과한 일수."""
    from datetime import datetime, timedelta

    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    elapsed_days = today.day  # 이달 들어 오늘까지 경과한 일수 (분모)

    try:
        logs = await db.fetchall(
            """SELECT logged_at FROM habit_logs
               WHERE habit_id = ? AND logged_at >= ? AND logged_at <= ?
               ORDER BY logged_at DESC""",
            (habit_id, month_start.isoformat(), today.isoformat()),
        )

        logged_days = {datetime.fromisoformat(log["logged_at"]).date() for log in logs}
        rate = len(logged_days) / max(1, elapsed_days)

        # streak 계산 (오늘부터 역추적, month_start 경계까지)
        streak = 0
        check_date = today
        while check_date >= month_start and check_date in logged_days:
            streak += 1
            check_date -= timedelta(days=1)

        return {"rate": rate, "streak": streak}

    except Exception as e:
        logger.error("completion_rate_failed: %s", e)
        return {"rate": 0.0, "streak": 0}


async def _analyze_patterns(db: DatabaseAdapter, habit_id: str) -> dict:
    """요일별 달성율 분석. 데이터 부족 시 명시적 메시지 반환."""
    from datetime import datetime

    try:
        logs = await db.fetchall(
            """SELECT logged_at FROM habit_logs WHERE habit_id = ?
               ORDER BY logged_at DESC LIMIT 100""",
            (habit_id,),
        )

        # 빈 데이터 edge case: max/min이 의미없는 0,0 반환하지 않도록 guard
        if not logs:
            return {
                "best_day": "데이터 부족",
                "worst_day": "데이터 부족",
                "total_logs": 0,
            }

        day_names = ["월", "화", "수", "목", "금", "토", "일"]
        day_counts = {i: 0 for i in range(7)}

        for log in logs:
            date_obj = datetime.fromisoformat(log["logged_at"]).date()
            day_idx = date_obj.weekday()
            day_counts[day_idx] += 1

        # 모든 값이 동일하면 best/worst 구분 무의미
        if len(set(day_counts.values())) == 1:
            return {
                "best_day": "편차 없음",
                "worst_day": "편차 없음",
                "total_logs": len(logs),
            }

        best_day = max(day_counts, key=day_counts.get)
        worst_day = min(day_counts, key=day_counts.get)

        return {
            "best_day": f"{day_names[best_day]} ({day_counts[best_day]}회)",
            "worst_day": f"{day_names[worst_day]} ({day_counts[worst_day]}회)",
            "total_logs": len(logs),
        }

    except Exception as e:
        logger.error("pattern_analysis_failed: %s", e)
        return {"best_day": "분석 실패", "worst_day": "분석 실패", "total_logs": 0}


async def _count_recent_failures(
    db: DatabaseAdapter, habit_id: str, days: int = 3
) -> int:
    """
    최근 days 일간 연속 미기록 일수 계산 (오늘 포함).
    days=3 이면 today, today-1, today-2 세 날짜만 체크 (최대 반환 3).
    첫 기록을 만나면 streak 종료.
    """
    from datetime import datetime, timedelta

    try:
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=days - 1)  # days개 연속 체크 범위 시작

        logs = await db.fetchall(
            """SELECT logged_at FROM habit_logs
               WHERE habit_id = ? AND logged_at >= ?
               ORDER BY logged_at DESC""",
            (habit_id, start_date.isoformat()),
        )

        logged_days = {datetime.fromisoformat(log["logged_at"]).date() for log in logs}

        # 오늘부터 정확히 days회 역추적 (off-by-one 방지)
        failure_count = 0
        check_date = today
        for _ in range(days):
            if check_date not in logged_days:
                failure_count += 1
            else:
                break
            check_date -= timedelta(days=1)

        return failure_count

    except Exception as e:
        logger.error("recent_failures_count_failed: %s", e)
        return 0
