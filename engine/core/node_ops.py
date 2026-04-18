"""노드 상태 전이 헬퍼 (S1-4).

executor.py·harness.py·cascade.py 등 여러 곳에서 `UPDATE nodes SET state=...,
stall_count=..., description=..., updated_at=... WHERE id=?` 패턴이 20+ 회
반복. 이 모듈로 추출해 1곳에서 관리.

**코어 파일(dag_advancer.py·state_machine.py) 수정 없이**, 기존 SQL 패턴과
100% 동등한 동작을 함수화한다. 각 호출부는 점진적으로 이 헬퍼로 교체.

사용 예:
    from engine.core.node_ops import mark_invalid_with_stall

    await mark_invalid_with_stall(
        db, node_id=task_node_id,
        description_json=json.dumps(verdict, ensure_ascii=False),
    )

주의:
- version·optimistic lock은 호출부에서 관리 (여기선 단순 UPDATE).
- trigger·cascade enqueue는 호출부 책임 (여기선 상태 write만).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    """UTC ISO 시각 (다른 곳과 통일 포맷)."""
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────────
# 상태 전이 헬퍼
# ────────────────────────────────────────────────────────────────────────

async def mark_invalid_with_stall(
    db: Any,
    node_id: str,
    description_json: str | None = None,
) -> None:
    """노드를 INVALID 로 전이 + stall_count 증가.

    QA FAIL 후 TASK·QA 재시도 트리거 패턴. executor.py 의 6곳 이상에서 중복.
    """
    now = _now()
    if description_json is not None:
        await db.execute(
            "UPDATE nodes SET state='INVALID', "
            "stall_count=COALESCE(stall_count,0)+1, "
            "description=?, updated_at=? WHERE id=?",
            (description_json, now, node_id),
        )
    else:
        await db.execute(
            "UPDATE nodes SET state='INVALID', "
            "stall_count=COALESCE(stall_count,0)+1, "
            "updated_at=? WHERE id=?",
            (now, node_id),
        )


async def mark_completed(
    db: Any,
    node_id: str,
    description_json: str | None = None,
) -> None:
    """노드를 COMPLETED 로 전이 + completed_at 기록."""
    now = _now()
    if description_json is not None:
        await db.execute(
            "UPDATE nodes SET state='COMPLETED', "
            "completed_at=?, updated_at=?, description=? WHERE id=?",
            (now, now, description_json, node_id),
        )
    else:
        await db.execute(
            "UPDATE nodes SET state='COMPLETED', "
            "completed_at=?, updated_at=? WHERE id=?",
            (now, now, node_id),
        )


async def mark_suspended(
    db: Any,
    node_id: str,
    verdict: dict | None = None,
) -> None:
    """노드를 SUSPENDED 로 전이 + verdict JSON 을 description 에 기록.

    stall_limit_exceeded / token_limit / circuit breaker 경로에서 사용.
    """
    now = _now()
    if verdict is not None:
        await db.execute(
            "UPDATE nodes SET state='SUSPENDED', description=?, updated_at=? WHERE id=?",
            (json.dumps(verdict, ensure_ascii=False), now, node_id),
        )
    else:
        await db.execute(
            "UPDATE nodes SET state='SUSPENDED', updated_at=? WHERE id=?",
            (now, node_id),
        )


async def mark_failed(
    db: Any,
    node_id: str,
    error_message: str | None = None,
) -> None:
    """노드를 FAILED 로 전이. dag_advancer가 보통 이 경로를 쓰지만 executor
    에서 직접 FAILED 마킹 케이스 대비.

    Stage 21: FAILED 전이 직후 failure_classifier 로 자동 분류 → failure_class
    컬럼 업데이트. watchdog 이 이 값을 활용해 TRANSIENT 자동 재개 / PERMANENT
    즉시 NEEDS_HUMAN 전이 결정.
    """
    now = _now()
    if error_message is not None:
        await db.execute(
            "UPDATE nodes SET state='FAILED', description=?, updated_at=? WHERE id=?",
            (error_message[:1000], now, node_id),
        )
    else:
        await db.execute(
            "UPDATE nodes SET state='FAILED', updated_at=? WHERE id=?",
            (now, node_id),
        )

    # Stage 21 classify 훅 (graceful — migration 027 미적용 시 silent skip)
    try:
        from engine.core.failure_classifier import mark_node_failure_class
        await mark_node_failure_class(db, node_id, error_message)
    except Exception:
        pass  # failure_class 컬럼 없거나 기타 오류는 무시 (방어적)


async def reset_to_ready(
    db: Any,
    node_id: str,
    clear_retry: bool = True,
    clear_stall: bool = True,
    clear_description: bool = False,
) -> None:
    """노드를 READY 로 리셋 (수동 재시도·watchdog 복구 경로).

    version+=1 로 optimistic lock 갱신.
    """
    now = _now()
    sets = ["state='READY'", "updated_at=?", "version=version+1"]
    params: list = [now]
    if clear_retry:
        sets.append("retry_count=0")
    if clear_stall:
        sets.append("stall_count=0")
    if clear_description:
        sets.append("description=NULL")
    sql = f"UPDATE nodes SET {', '.join(sets)} WHERE id=?"
    params.append(node_id)
    await db.execute(sql, tuple(params))


# ────────────────────────────────────────────────────────────────────────
# 쌍 (TASK+QA) 조작
# ────────────────────────────────────────────────────────────────────────

async def invalidate_task_and_qa(
    db: Any,
    task_id: str,
    qa_id: str,
    qa_verdict_json: str,
    task_verdict_json: str | None = None,
) -> None:
    """QA FAIL 시점에 QA 와 paired TASK 를 동시에 INVALID 로.

    executor.py 의 모든 QA FAIL 경로 (harness_structural/interactivity/
    design_match/document/json/chunked_json)에서 동일 패턴이 반복돼 이 헬퍼로
    묶는다. TASK description 은 별도 제공 가능 (없으면 QA verdict 공유).
    """
    await mark_invalid_with_stall(db, node_id=qa_id, description_json=qa_verdict_json)
    await mark_invalid_with_stall(
        db, node_id=task_id,
        description_json=(task_verdict_json or qa_verdict_json),
    )
