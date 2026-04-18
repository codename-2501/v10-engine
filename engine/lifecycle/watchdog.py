"""
engine/lifecycle/watchdog.py
Periodic watchdog that detects and resets stuck nodes.

Runs as an async background task alongside DAGAdvancer.

IN_PROGRESS stuck detection (every 5 min):
- TASK stuck > 30 min, QA stuck > 10 min, no live agent pid, no recent heartbeat
- Action: reset to NOT_STARTED (retry+1) or FAILED if max_retries reached

SUSPENDED transient-error recovery (every 5 min):
- suspension_reason matches transient patterns (529/429/overloaded/timeout/rate limit)
- Minimum cooldown elapsed since suspension (avoid hammering the upstream)
- Action: revert to NOT_STARTED and clear suspension_reason so DAG picks it up
- Permanent-error suspensions (quota, auth, SHUTDOWN_DRAIN) are left untouched
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from engine.observability.logger import get_logger

logger = get_logger(__name__)

WATCHDOG_INTERVAL_SECONDS = 300  # 5 minutes
TASK_STUCK_THRESHOLD_MINUTES = 30
QA_STUCK_THRESHOLD_MINUTES = 10
MAX_RETRIES = 3

# 일시 오류 패턴 — 이 사유로 SUSPENDED 된 노드는 자동 재개 대상
_TRANSIENT_SUSPENSION_PATTERNS = re.compile(
    r"(529|overloaded|rate[_\s-]*limit|429|timeout|timed\s*out|"
    r"service\s*unavailable|temporarily|econnreset|econnrefused|"
    r"socket hang up|gateway|bad\s*gateway|502|503|504|"
    r"enotfound|getaddrinfo|unable to connect|connection\s+reset|"
    r"connection\s+refused|network\s+error|dns)",
    re.IGNORECASE,
)
# 영구 오류 패턴 — 재개 금지
_PERMANENT_SUSPENSION_PATTERNS = re.compile(
    r"(hit\s+your\s+limit|quota|insufficient|invalid[_\s-]*api[_\s-]*key|"
    r"unauthori[sz]ed|forbidden|authentication|credit|billing)",
    re.IGNORECASE,
)
# 엔진 내부 상태로 쓰이는 특수 사유 — watchdog이 건드리지 않음 (startup 등 다른 경로가 처리)
_RESERVED_SUSPENSION_REASONS = frozenset({
    "SHUTDOWN_DRAIN",
})

SUSPENDED_MIN_COOLDOWN_MINUTES = 3  # suspend 직후 바로 풀면 또 같은 오류 → 쿨다운
# 같은 노드가 일시 오류로 SUSPENDED ↔ NOT_STARTED 를 왕복하는 횟수 상한.
# 초과 시 재개 중단 → 사람이 개입하거나 원인 해결 후 수동 재가동.
SUSPENDED_MAX_RESUMES = 3
# failure_reasons JSON 배열에서 이 크기를 넘어가면 일시 오류 무한 시도로 판단
# (대략 attempt 수로 환산: 하나당 300~500B → 6000B 는 약 12~20회)
_RESUME_RUNAWAY_FR_BYTES = 6000


async def run_watchdog(db, dag_advancer=None) -> None:
    """Background loop — runs every WATCHDOG_INTERVAL_SECONDS.

    dag_advancer(optional): 상태 복원 후 해당 DAG 를 enqueue 해서 advancer 가
    즉시 픽업하도록 한다. None 이면 DB 만 업데이트하고 advancer 가 다음 순회에
    자연 감지하길 기대 (대기 시간 길어짐).
    """
    logger.info("watchdog_started interval=%ds", WATCHDOG_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            await _check_stuck_nodes(db, dag_advancer)
            await _recover_transient_suspended(db, dag_advancer)
            await _recover_transient_failed(db, dag_advancer)
            await _detect_suspension_miss(db)
        except asyncio.CancelledError:
            logger.info("watchdog_stopped")
            break
        except Exception as e:
            logger.warning("watchdog_error: %s", e)


async def _detect_suspension_miss(db) -> None:
    """
    코어 5파일 P0 보강 — suspension state update miss 간접 감지.

    dag_advancer.py:332 의 bare except 로 API 한도 에러 후 SUSPENDED 전이가
    silent 실패하는 경우, 노드는 IN_PROGRESS 그대로 남는다. 이것은 일반
    zombie 감지와 구분되어야 의미 있으므로, 다음 조건으로 감지:
      - state='IN_PROGRESS'
      - updated_at 5분 초과 (일반 실행 타임아웃보다 짧음)
      - failure_reasons 에 "429|rate_limit|api 한도|limit" 키워드

    코어 파일 수정 없이 보강. 감지만 하고 상태 변경은 기존 _check_stuck_nodes
    가 담당.
    """
    try:
        rows = await db.fetchall(
            """SELECT id, name, updated_at, failure_reasons
               FROM nodes
               WHERE state = 'IN_PROGRESS'
                 AND (julianday('now') - julianday(updated_at)) * 24 * 60 > 5
               LIMIT 20"""
        )
        for r in rows:
            fr = (r.get("failure_reasons") or "").lower()
            # rate-limit 류 키워드가 있으면 suspension 누락일 가능성
            if any(kw in fr for kw in ("429", "rate_limit", "api 한도", "limit exceeded")):
                logger.warning(
                    "possible_suspension_miss node=%s name=%s last_update=%s",
                    r["id"][:8], (r["name"] or "")[:40], r["updated_at"],
                )
    except Exception as exc:
        logger.debug("suspension_miss_detection_skipped: %s", exc)


async def _enqueue_dags_for_nodes(db, dag_advancer, node_ids: list[str]) -> None:
    """복원된 노드들의 dag_id 를 중복 제거 후 advancer 큐에 투입."""
    if not dag_advancer or not node_ids:
        return
    try:
        ph = ",".join(["?"] * len(node_ids))
        rows = await db.fetchall(
            f"SELECT DISTINCT dag_id FROM nodes WHERE id IN ({ph})",
            node_ids,
        )
        for r in rows:
            if r["dag_id"]:
                await dag_advancer.enqueue(r["dag_id"])
    except Exception as exc:
        logger.debug("enqueue_dags_for_nodes skipped: %s", exc)


def _extract_latest_failure_text(failure_reasons_json: str | None) -> str:
    """failure_reasons JSON 문자열에서 가장 최근 attempt의 reason 텍스트를 꺼낸다.

    executor가 suspension_reason을 비워두고 failure_reasons에만 오류 본문을
    기록하는 경로가 있어서, 재개 판정 시 이 텍스트도 함께 살핀다.
    파싱 실패 / 빈 리스트 → "" 반환.
    """
    if not failure_reasons_json:
        return ""
    try:
        import json as _json
        items = _json.loads(failure_reasons_json) or []
        if not items:
            return ""
        last = items[-1]
        if isinstance(last, dict):
            return str(last.get("reason", ""))
        return str(last)
    except Exception:
        return ""


def _is_transient_suspension(
    reason: str | None,
    failure_reasons_json: str | None = None,
) -> bool:
    """SUSPENDED 노드가 일시 오류(재개 대상)인지 판정.

    판정 규칙:
    - RESERVED 사유 → watchdog 관할 아님
    - PERMANENT 패턴 매칭 → 영구 오류, 재개 금지
    - TRANSIENT 패턴 (suspension_reason 또는 failure_reasons 최신 텍스트) → 재개
    - 어느 쪽에서도 단서 없음 → 재개하지 않음 (안전)
    """
    if reason in _RESERVED_SUSPENSION_REASONS:
        return False
    combined = " ".join(x for x in (reason or "", _extract_latest_failure_text(failure_reasons_json)) if x)
    if not combined.strip():
        return False
    if _PERMANENT_SUSPENSION_PATTERNS.search(combined):
        return False
    return bool(_TRANSIENT_SUSPENSION_PATTERNS.search(combined))


def _is_bare_suspension(
    reason: str | None,
    failure_reasons_json: str | None = None,
) -> bool:
    """
    사유 미기록 SUSPENDED 판정 (QA cascade / 프로그래매틱 revert 추정).

    suspension_reason 비어있음 + failure_reasons 비어있음 = QA 검증 실패로
    executor가 노드를 되돌렸으나 이유 텍스트를 남기지 않은 경우. watchdog
    자동 재개가 막혀 노드가 무한 정지하는 문제(2026-04-18 Habit Tracker 실행
    101분 방치 사례) 해결.

    retry_count 증가 + SUSPENDED_MAX_RESUMES 제한은 호출부에서 적용.
    """
    if reason and reason.strip():
        return False
    if reason in _RESERVED_SUSPENSION_REASONS:
        return False
    try:
        import json as _json
        items = _json.loads(failure_reasons_json or "[]") or []
        return len(items) == 0
    except Exception:
        return False


async def _recover_transient_suspended(db, dag_advancer=None) -> int:
    """일시 오류로 SUSPENDED된 노드를 NOT_STARTED로 복원. 반환: 복원 개수.

    범용 로직 — 프로젝트/노드 이름에 의존하지 않고 state + suspension_reason만 본다.
    """
    now = _now()

    rows = await db.fetchall(
        f"""SELECT id, name, node_type, suspension_reason, failure_reasons,
                   COALESCE(retry_count, 0) AS retry_count,
                   (julianday('now') - julianday(updated_at)) * 1440 AS idle_minutes
            FROM nodes
            WHERE state = 'SUSPENDED'
            LIMIT 500"""
    )

    resumed_ids: list[str] = []
    skipped_runaway: list[str] = []
    for row in rows:
        reason = row["suspension_reason"]
        fr = row["failure_reasons"] or ""
        is_transient = _is_transient_suspension(reason, fr)
        is_bare = _is_bare_suspension(reason, fr)
        # transient (일시 오류) OR bare (사유 미기록) 모두 재개 대상
        if not (is_transient or is_bare):
            continue
        if (row["idle_minutes"] or 0) < SUSPENDED_MIN_COOLDOWN_MINUTES:
            continue
        # 무한 재개 루프 방지: failure_reasons 크기로 누적 시도 횟수 근사
        if len(fr) > _RESUME_RUNAWAY_FR_BYTES:
            skipped_runaway.append(row["id"])
            continue
        # bare suspension의 경우 retry_count 엄격 제한 (안전장치)
        if is_bare and row["retry_count"] >= SUSPENDED_MAX_RESUMES:
            skipped_runaway.append(row["id"])
            continue
        resumed_ids.append(row["id"])

    if skipped_runaway:
        logger.warning(
            "watchdog_transient_runaway_skip count=%d — needs human",
            len(skipped_runaway),
        )

    if not resumed_ids:
        return 0

    ph = ",".join(["?"] * len(resumed_ids))
    # retry_count 는 **의도적으로 보존**한다 — executor의 근본원인 분석(Sonnet)이
    # node.retry_count > 0 조건을 쓰기 때문. 여기서 0으로 리셋하면 분석이 스킵된다.
    # stall_count 와 suspension_reason 만 정리해서 DAG 가 다시 픽업하게 한다.
    await db.execute(
        f"""UPDATE nodes
            SET state = 'NOT_STARTED',
                suspension_reason = NULL,
                stall_count = 0,
                updated_at = ?,
                version = version + 1
            WHERE id IN ({ph}) AND state = 'SUSPENDED'""",
        [now] + resumed_ids,
    )
    logger.info(
        "watchdog_transient_resumed count=%d sample=%s",
        len(resumed_ids),
        ",".join(i[:8] for i in resumed_ids[:5]) + ("..." if len(resumed_ids) > 5 else ""),
    )
    # advancer 에 해당 DAG enqueue → 다음 순회에서 즉시 READY/IN_PROGRESS 전환
    await _enqueue_dags_for_nodes(db, dag_advancer, resumed_ids)
    return len(resumed_ids)


def _count_structural_failures(failure_reasons_json: str | None) -> int:
    """failure_reasons 중 구조적 결함(재시도해도 안 고쳐질 가능성) 카운트.

    missing_section, schema, validation, 금지어, forbidden 키워드 기반.
    일시 오류와 구별해 자동 복구 대상 판정용.
    """
    if not failure_reasons_json:
        return 0
    try:
        items = json.loads(failure_reasons_json) if isinstance(failure_reasons_json, str) else failure_reasons_json
        if not isinstance(items, list):
            return 0
    except Exception:
        return 0
    count = 0
    import re as _re
    pat = _re.compile(
        r"(missing[_\s]?section|누락|schema|validation|금지어|forbidden|invalid\s+structure)",
        _re.IGNORECASE,
    )
    for it in items:
        text = str(it.get("reason", "")) if isinstance(it, dict) else str(it)
        if pat.search(text):
            count += 1
    return count


async def _recover_transient_failed(db, dag_advancer=None) -> int:
    """FAILED 상태지만 **일시 오류 사유**인 노드를 NOT_STARTED로 복원.

    조건 (AND):
      - state = 'FAILED'
      - failure_reasons 최신 메시지가 _TRANSIENT 패턴에 매치 (_is_transient_suspension 활용)
      - 구조적 결함 (_count_structural_failures) < 3  — 3회+는 구조 결함 확정
      - updated_at 경과 >= SUSPENDED_MIN_COOLDOWN_MINUTES

    SUSPENDED와 달리 FAILED는 dag_advancer가 retry 소진으로 확정한 상태라
    기존에는 수동 개입 필요. 일시 장애가 retry_count 소비해 FAILED로 간 경우를
    자동 복구.

    retry_count 는 보존 (새 시작 아님, 일시 장애 극복).
    """
    now = _now()

    rows = await db.fetchall(
        """SELECT id, name, node_type, failure_reasons,
                  COALESCE(retry_count, 0) AS retry_count,
                  (julianday('now') - julianday(updated_at)) * 1440 AS idle_minutes
           FROM nodes
           WHERE state = 'FAILED'
           LIMIT 500"""
    )

    resumed_ids: list[str] = []
    skipped_structural: list[str] = []
    skipped_runaway: list[str] = []
    for row in rows:
        fr = row["failure_reasons"] or ""
        # 일시 오류 매치 (suspension_reason은 FAILED에 없으니 fr만)
        if not _is_transient_suspension(None, fr):
            continue
        if (row["idle_minutes"] or 0) < SUSPENDED_MIN_COOLDOWN_MINUTES:
            continue
        # 구조 결함 3회+ 제외
        struct_count = _count_structural_failures(fr)
        if struct_count >= 3:
            skipped_structural.append(row["id"])
            continue
        if len(fr) > _RESUME_RUNAWAY_FR_BYTES:
            skipped_runaway.append(row["id"])
            continue
        resumed_ids.append(row["id"])

    if skipped_structural:
        logger.warning(
            "watchdog_failed_structural_skip count=%d — structural failures≥3",
            len(skipped_structural),
        )
    if skipped_runaway:
        logger.warning(
            "watchdog_failed_runaway_skip count=%d — needs human",
            len(skipped_runaway),
        )

    if not resumed_ids:
        return 0

    ph = ",".join(["?"] * len(resumed_ids))
    # retry_count 보존 — 일시 장애 극복 시도임을 유지 (stall_count도 리셋 안 함)
    await db.execute(
        f"""UPDATE nodes
            SET state = 'NOT_STARTED',
                updated_at = ?,
                version = version + 1
            WHERE id IN ({ph}) AND state = 'FAILED'""",
        [now] + resumed_ids,
    )
    logger.info(
        "watchdog_failed_transient_resumed count=%d sample=%s",
        len(resumed_ids),
        ",".join(i[:8] for i in resumed_ids[:5]) + ("..." if len(resumed_ids) > 5 else ""),
    )
    await _enqueue_dags_for_nodes(db, dag_advancer, resumed_ids)
    return len(resumed_ids)


async def _check_stuck_nodes(db, dag_advancer=None) -> int:
    """Find and reset stuck IN_PROGRESS nodes. Returns number of resets."""
    now = _now()
    reset_count = 0
    reset_ids: list[str] = []

    # Query all IN_PROGRESS nodes with their stuck duration in minutes.
    # Uses COALESCE(last_heartbeat, updated_at) as the reference timestamp.
    rows = await db.fetchall(
        """SELECT id, name, project_id, dag_id, node_type,
                  COALESCE(retry_count, 0) AS retry_count,
                  (julianday('now') - julianday(COALESCE(last_heartbeat, updated_at))) * 1440
                      AS stuck_minutes
           FROM nodes
           WHERE state = 'IN_PROGRESS'
           LIMIT 100"""
    )

    for node in rows:
        node_type = node["node_type"]
        stuck_minutes = node["stuck_minutes"] or 0

        # Apply per-type threshold
        if node_type == "QA":
            threshold = QA_STUCK_THRESHOLD_MINUTES
        else:
            threshold = TASK_STUCK_THRESHOLD_MINUTES

        if stuck_minutes < threshold:
            continue

        # Check if agent process is actually running — skip if alive
        if await _check_agent_alive(db, node["id"]):
            continue

        retries = node["retry_count"] or 0

        if retries >= MAX_RETRIES:
            # Too many retries → FAILED (prevent infinite loops)
            await db.execute(
                """UPDATE nodes
                   SET state = 'FAILED',
                       updated_at = ?,
                       version = version + 1
                   WHERE id = ? AND state = 'IN_PROGRESS'""",
                (now, node["id"]),
            )
            logger.warning(
                "watchdog_max_retries node=%s name=%s retries=%d stuck_min=%.0f",
                node["id"][:8], node["name"], retries, stuck_minutes,
            )
        else:
            # Reset to NOT_STARTED for clean re-execution
            await db.execute(
                """UPDATE nodes
                   SET state = 'NOT_STARTED',
                       retry_count = COALESCE(retry_count, 0) + 1,
                       updated_at = ?,
                       version = version + 1
                   WHERE id = ? AND state = 'IN_PROGRESS'""",
                (now, node["id"]),
            )

            # Also reset the paired QA/TASK node if it exists and isn't COMPLETED
            await _reset_pair_node(db, node["id"], now)

            reset_count += 1
            reset_ids.append(node["id"])
            logger.info(
                "watchdog_reset node=%s name=%s type=%s stuck_min=%.0f retries=%d",
                node["id"][:8], node["name"], node_type,
                stuck_minutes, retries + 1,
            )

    if reset_count:
        logger.info("watchdog_cycle_done resets=%d", reset_count)
        await _enqueue_dags_for_nodes(db, dag_advancer, reset_ids)

    return reset_count


async def _reset_pair_node(db, node_id: str, now: str) -> None:
    """Reset the paired QA or TASK node if it exists and isn't COMPLETED."""
    pair = await db.fetchone(
        """SELECT id FROM nodes
           WHERE (task_pair_node_id = ? OR qa_pair_node_id = ?)
             AND state NOT IN ('COMPLETED', 'SKIPPED')""",
        (node_id, node_id),
    )
    if pair:
        await db.execute(
            """UPDATE nodes
               SET state = 'NOT_STARTED',
                   updated_at = ?,
                   version = version + 1
               WHERE id = ? AND state NOT IN ('COMPLETED', 'SKIPPED')""",
            (now, pair["id"]),
        )
        logger.info("watchdog_pair_reset pair_node=%s", pair["id"][:8])


async def _check_agent_alive(db, node_id: str) -> bool:
    """Check if an agent process for this node is actually running (OS pid check)."""
    proc = await db.fetchone(
        """SELECT pid FROM agent_processes
           WHERE node_id = ? AND status IN ('ALIVE', 'STARTING', 'RETRYING')
           ORDER BY rowid DESC LIMIT 1""",
        (node_id,),
    )
    if not proc or not proc.get("pid"):
        return False
    try:
        os.kill(int(proc["pid"]), 0)  # Signal 0 = existence check only
        return True
    except (OSError, ValueError):
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
