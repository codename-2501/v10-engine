"""Phase 0 Contract Framework (S3-1).

각 단계(DEFINE/DESIGN/BUILD/VERIFY/DELIVER) 종료 시점에 "단계 결과물이
계약을 충족하는가?" 검증. 실패 시 다음 단계 GATE 노드를 AWAITING_APPROVAL
로 차단.

코어(dag_advancer/state_machine/cascade) 무수정 — executor 의 노드 COMPLETED
경로 또는 cascade phase2 종료 직후 hook 으로 호출.

설계:
- 계약은 "단계가 산출해야 할 핵심 artifacts 가 모두 존재 + 최소 품질 충족"
- 실패 시 contract_violations 테이블에 사유 기록 + 다음 GATE 차단 신호
- 코어 변경 없이 차단 — 다음 GATE 노드를 AWAITING_APPROVAL 로 마킹

사용 예:
    from engine.core.phase_contract import check_phase_contract

    result = await check_phase_contract(db, engagement_id, phase="DEFINE")
    if not result.passed:
        # 다음 단계 GATE 차단 (AWAITING_APPROVAL)
        await block_next_gate(db, engagement_id, phase="DEFINE",
                              reason=result.summary)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ContractResult:
    phase: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    checks_performed: list[str] = field(default_factory=list)
    summary: str = ""


# 단계별 필수 artifact 키워드 (task_name 부분 매칭).
# 정확한 spec 명을 강제하지 않고 키워드로 → 다양한 명명 변종 수용.
PHASE_REQUIRED_ARTIFACTS: dict[str, list[str]] = {
    "DEFINE": [
        "요구사항", "기능 백로그", "유스케이스",
        "화면 목록", "리스크",
    ],
    "DESIGN": [
        "정보 구조", "유저플로우", "화면 설계",
        "디자인 토큰", "ui 디자인", "api 설계",
        "db 설계",
    ],
    "BUILD": [
        "페이지 레시피", "페이지 조립",
    ],
    "VERIFY": [
        "테스트", "검증",
    ],
    "DELIVER": [
        "배포", "산출",
    ],
}


PHASE_MIN_PASS_RATIO: dict[str, float] = {
    # phase 별 COMPLETED 노드 / 전체 노드 비율 최소값
    "DEFINE": 0.85,
    "DESIGN": 0.80,
    "BUILD": 0.75,
    "VERIFY": 0.90,
    "DELIVER": 0.95,
}


async def check_phase_contract(
    db: Any,
    engagement_id: str,
    phase: str,
) -> ContractResult:
    """phase 의 모든 노드 상태 검사 → ContractResult."""
    result = ContractResult(phase=phase, passed=False)
    if db is None:
        result.violations.append("db unavailable")
        return result

    try:
        # 1) 해당 phase 의 노드 전수 조회
        rows = await db.fetchall(
            """SELECT id, task_name, type, state FROM nodes
            WHERE engagement_id=? AND phase=?""",
            (engagement_id, phase),
        )
        nodes = [dict(r) for r in rows]
        if not nodes:
            result.violations.append(f"phase {phase} 노드 0건")
            return result

        # 2) COMPLETED 비율 체크
        total = len(nodes)
        completed = sum(1 for n in nodes if n["state"] == "COMPLETED")
        ratio = completed / total
        result.checks_performed.append(
            f"completed_ratio: {completed}/{total} = {ratio:.0%}"
        )
        min_ratio = PHASE_MIN_PASS_RATIO.get(phase, 0.8)
        if ratio < min_ratio:
            result.violations.append(
                f"COMPLETED 비율 {ratio:.0%} < 임계 {min_ratio:.0%}"
            )

        # 3) 필수 artifact 존재 체크
        required = PHASE_REQUIRED_ARTIFACTS.get(phase, [])
        node_names_lower = [(n.get("task_name") or "").lower() for n in nodes]
        for kw in required:
            kw_l = kw.lower()
            present = any(kw_l in name for name in node_names_lower)
            if not present:
                result.violations.append(f"필수 artifact 누락: '{kw}'")
        result.checks_performed.append(
            f"required_artifacts: {len(required)}개 검사"
        )

        # 4) FAILED/SUSPENDED 노드 0 건이어야 함
        bad = [n for n in nodes if n["state"] in ("FAILED", "SUSPENDED")]
        if bad:
            result.violations.append(
                f"FAILED/SUSPENDED 노드 {len(bad)}건: "
                + ", ".join(n["task_name"][:30] for n in bad[:3])
            )
        result.checks_performed.append(f"failed_suspended: {len(bad)}건")

        result.passed = len(result.violations) == 0
        result.summary = (
            f"PASS — {phase} 통과 ({completed}/{total})"
            if result.passed
            else f"FAIL — {phase}: {len(result.violations)}건 위반"
        )
    except Exception as e:
        logger.warning("check_phase_contract(%s) failed: %s", phase, e)
        result.violations.append(f"체크 중 예외: {e}")

    return result


async def record_violation(
    db: Any, engagement_id: str, phase: str, result: ContractResult,
) -> None:
    """위반 사항을 DB 에 영구 기록 (audit + 대시보드용)."""
    if db is None or result.passed:
        return
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contract_violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, engagement_id TEXT, phase TEXT,
                violations TEXT, summary TEXT
            )
        """)
        await db.execute(
            """INSERT INTO contract_violations(ts, engagement_id, phase, violations, summary)
            VALUES(?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                engagement_id, phase,
                json.dumps(result.violations, ensure_ascii=False),
                result.summary,
            ),
        )
    except Exception as e:
        logger.warning("record_violation failed: %s", e)


async def block_next_gate(
    db: Any, engagement_id: str, phase: str, reason: str,
) -> bool:
    """phase 직후 GATE 노드를 AWAITING_APPROVAL 로 차단.

    GATE 노드 검색: type='GATE' AND phase=다음 phase. 없으면 no-op.
    state_machine 직접 변경 X — 단순 UPDATE (코어 미터치).
    """
    next_phase = _next_phase(phase)
    if not next_phase:
        return False
    try:
        rows = await db.fetchall(
            """SELECT id FROM nodes
            WHERE engagement_id=? AND phase=? AND type='GATE'
              AND state IN ('NOT_STARTED','READY')""",
            (engagement_id, next_phase),
        )
        gate_ids = [r["id"] for r in rows]
        if not gate_ids:
            return False
        now = datetime.now(timezone.utc).isoformat()
        for gid in gate_ids:
            await db.execute(
                """UPDATE nodes SET state='AWAITING_APPROVAL',
                description=?, updated_at=? WHERE id=?""",
                (
                    json.dumps(
                        {"contract_violation": True, "phase": phase,
                         "reason": reason},
                        ensure_ascii=False,
                    ),
                    now, gid,
                ),
            )
        logger.info(
            "phase_contract_blocked phase=%s gates=%d reason=%s",
            phase, len(gate_ids), reason[:120],
        )
        return True
    except Exception as e:
        logger.warning("block_next_gate failed: %s", e)
        return False


_PHASE_ORDER = ["DEFINE", "DESIGN", "BUILD", "VERIFY", "DELIVER"]


def _next_phase(phase: str) -> str | None:
    try:
        i = _PHASE_ORDER.index(phase)
        if i + 1 < len(_PHASE_ORDER):
            return _PHASE_ORDER[i + 1]
    except ValueError:
        pass
    return None


async def enforce_after_phase(
    db: Any, engagement_id: str, phase: str,
) -> ContractResult:
    """원샷 헬퍼 — 검사 + 위반 기록 + GATE 차단까지 일괄.

    cascade phase2 완료 또는 노드 완료 hook 에서 호출.
    """
    result = await check_phase_contract(db, engagement_id, phase)
    if not result.passed:
        await record_violation(db, engagement_id, phase, result)
        await block_next_gate(db, engagement_id, phase, result.summary)
    return result
