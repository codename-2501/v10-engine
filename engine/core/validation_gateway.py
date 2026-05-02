"""
engine/core/validation_gateway.py
ValidationGateway — C1~C9 케이스 처리.
C1: DAG 순환 → CycleError
C2: 위상 정렬 위반 → TopoViolationError
C3: 의존성 INVALID → 노드 BLOCKED
C4: 요구사항 대규모 변경 → ImpactAnalyzer
C5: 헌법 버전 업그레이드 → ConflictDetector
C6: 동시 수정 충돌 → GateConflictError(409)
C7: 예산 초과 예상 → BudgetEnforcer L1 사전 차단
C8: 헌법 활성화 → 단계적 전환
C9: 수동 재실행 → INVALID → READY
"""

from __future__ import annotations

import logging
from enum import Enum

from engine.core.state_machine import NodeState, StateMachine, VALID_TRANSITIONS
from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """ValidationGateway 검증 실패 기본 예외."""
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class CycleError(ValidationError):
    """C1: DAG 순환 참조 감지."""
    def __init__(self, message: str = "DAG 순환 참조 감지 (Rule 1 위반)") -> None:
        super().__init__("CYCLE_DETECTED", message)


class TopoViolationError(ValidationError):
    """C2: 위상 정렬 위반."""
    def __init__(self, message: str) -> None:
        super().__init__("TOPO_VIOLATION", message)


class GateConflictError(ValidationError):
    """C6: 동시 수정 충돌 (낙관적 잠금 실패)."""
    def __init__(self, current_version: int) -> None:
        super().__init__(
            "CONCURRENT_MODIFICATION",
            "동시 수정 충돌 — 최신 버전을 재로드 후 재시도",
            {"current_version": current_version},
        )


class BudgetPreemptionError(ValidationError):
    """C7: BudgetEnforcer L1 사전 차단 — 예산 초과 예상."""
    def __init__(self, model: str, estimated: int, limit: int) -> None:
        super().__init__(
            "BUDGET_PREEMPTION",
            f"예상 토큰({estimated:,}) > L1 한도({limit:,}) — 실행 차단",
            {"model": model, "estimated_tokens": estimated, "limit": limit},
        )


class ConstitutionConflictError(ValidationError):
    """C8: 헌법 버전 업그레이드 충돌 — 단계적 전환 필요."""
    def __init__(self, conflict_count: int, blocking: bool) -> None:
        super().__init__(
            "CONSTITUTION_CONFLICT",
            f"헌법 충돌 {conflict_count}건 — {'전환 차단' if blocking else '단계적 전환 필요'}",
            {"conflict_count": conflict_count, "blocking": blocking},
        )


# ---------------------------------------------------------------------------
# ValidationGateway
# ---------------------------------------------------------------------------

class ValidationGateway:
    """
    C1~C9 케이스 처리.
    각 메서드는 독립 호출 가능.
    """

    def __init__(self, db: DatabaseAdapter) -> None:
        self._db = db

    # C1: DAG 순환 검사 (topological_sort 결과 재활용)
    async def check_c1_no_cycle(self, dag_id: str) -> None:
        """
        edges 테이블에서 순환 참조 감지.
        감지 시 CycleError 발생.
        """
        rows = await self._db.fetchall(
            "SELECT from_node_id, to_node_id FROM edges WHERE dag_id=? AND is_active=1",
            (dag_id,),
        )
        # DFS 기반 순환 탐지
        graph: dict[str, list[str]] = {}
        for r in rows:
            graph.setdefault(r["from_node_id"], []).append(r["to_node_id"])

        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in list(graph.keys()):
            if node not in visited:
                if dfs(node):
                    raise CycleError()

    # C2: 실행 시도 노드의 위상 정렬 위반 검사
    async def check_c2_topo_order(
        self, node_id: str, dag_id: str
    ) -> None:
        """선행 노드가 모두 COMPLETED인지 확인."""
        edge_rows = await self._db.fetchall(
            """SELECT n.id, n.state, n.name
               FROM edges e
               JOIN nodes n ON n.id = e.from_node_id
               WHERE e.to_node_id=? AND e.is_active=1""",
            (node_id,),
        )
        incomplete = [r for r in edge_rows if r["state"] not in (NodeState.COMPLETED, NodeState.SKIPPED)]
        if incomplete:
            names = [r["name"] for r in incomplete]
            raise TopoViolationError(
                f"선행 노드 미완료: {names} — Rule 3 위반"
            )

    # C3: 의존성 INVALID → 대상 노드 BLOCKED
    async def check_c3_deps_invalid(self, node_id: str) -> bool:
        """
        INVALID 의존 노드 존재 시 대상 노드를 BLOCKED로 전환.
        반환: BLOCKED 전환 여부.
        """
        rows = await self._db.fetchall(
            """SELECT n.state FROM edges e
               JOIN nodes n ON n.id = e.from_node_id
               WHERE e.to_node_id=? AND e.is_active=1
                 AND n.state='INVALID'""",
            (node_id,),
        )
        if not rows:
            return False

        node = await self._db.fetchone(
            "SELECT state, version FROM nodes WHERE id=?", (node_id,)
        )
        if not node:
            return False

        if StateMachine.is_valid(node["state"], NodeState.BLOCKED):
            await self._db.execute(
                """UPDATE nodes SET state='BLOCKED', updated_at=datetime('now'),
                   version=version+1 WHERE id=? AND version=?""",
                (node_id, node["version"]),
            )
            logger.info("c3_node_blocked node_id=%s", node_id)
            return True
        return False

    # C6: 낙관적 잠금 충돌 검사
    async def check_c6_version(
        self, node_id: str, expected_version: int
    ) -> None:
        """version 불일치 시 GateConflictError(409)."""
        row = await self._db.fetchone(
            "SELECT version FROM nodes WHERE id=?", (node_id,)
        )
        if not row:
            return
        if row["version"] != expected_version:
            raise GateConflictError(current_version=row["version"])

    # C4: 요구사항 대규모 변경 → ImpactAnalyzer 배치 실행
    async def check_c4_impact_analysis(
        self,
        requirement_id: str,
        old_version: int,
        new_version: int,
        change_summary: str = "",
    ) -> "ImpactAnalysisResult":
        """
        요구사항 버전 변경 시 downstream 노드 영향 분석.
        CRITICAL/HIGH 심각도 노드는 Cascade Invalidation Phase1 자동 예약.
        반환: ImpactAnalysisResult (분석 결과 + 예약된 무효화 건수)
        """
        from engine.core.impact_analyzer import ImpactAnalyzer, ImpactAnalysisResult
        analyzer = ImpactAnalyzer(self._db)
        result = await analyzer.analyze(
            requirement_id=requirement_id,
            old_version=old_version,
            new_version=new_version,
            change_summary=change_summary,
        )
        logger.info(
            "c4_impact_analysis_done requirement_id=%s total_impacted=%s critical=%s invalidation_scheduled=%s",
            requirement_id,
            len(result.impacted_nodes),
            result.total_critical,
            result.invalidation_scheduled,
        )
        return result

    # C5: 헌법 버전 업그레이드 → ConflictDetector
    async def check_c5_conflict_detection(
        self,
        old_version_id: str,
        new_version_id: str,
    ) -> "DetectionResult":
        """
        헌법 버전 업그레이드 전 충돌 감지.
        blocking 충돌 시 ConstitutionConflictError 발생.
        non-blocking 충돌 시 결과 반환 (단계적 전환 필요 플래그 포함).
        """
        from engine.core.conflict_detector import ConflictDetector, DetectionResult
        detector = ConflictDetector(self._db)
        result = await detector.detect(
            old_version_id=old_version_id,
            new_version_id=new_version_id,
        )
        if result.has_blocking_conflict:
            raise ConstitutionConflictError(
                conflict_count=len(result.conflicts),
                blocking=True,
            )
        logger.info(
            "c5_conflict_detection_done old=%s new=%s conflicts=%s staged=%s",
            old_version_id, new_version_id,
            len(result.conflicts),
            result.staged_transition_required,
        )
        return result

    # C7: 예산 초과 예상 → BudgetEnforcer L1 사전 차단
    async def check_c7_budget_preemption(
        self,
        node_id: str,
        model: str,
        prompt_text: str,
        project_id: str,
        phase: str,
    ) -> None:
        """
        노드 실행 전 예산 L1(Pre-call 추정) 검사.
        추정 토큰이 한도를 초과하면 BudgetPreemptionError 발생 → 노드 SUSPENDED.
        """
        from engine.core.budget_enforcer import BudgetEnforcer, InputBudgetExceededError
        enforcer = BudgetEnforcer(self._db)
        try:
            await enforcer.pre_call_check(node_id, project_id, phase, prompt_text)
        except InputBudgetExceededError as exc:
            # 노드를 SUSPENDED로 전환
            node = await self._db.fetchone(
                "SELECT state, version FROM nodes WHERE id=?", (node_id,)
            )
            if node and StateMachine.is_valid(node["state"], NodeState.SUSPENDED):
                await self._db.execute(
                    """UPDATE nodes
                       SET state='SUSPENDED', suspension_reason='BUDGET_EXCEEDED',
                           updated_at=datetime('now'), version=version+1
                       WHERE id=? AND version=?""",
                    (node_id, node["version"]),
                )
                logger.warning("c7_node_suspended_budget node_id=%s model=%s", node_id, model)
            # 추정값 파싱 (message에서 숫자 추출 시도)
            estimated = getattr(exc, "estimated", 0)
            limit = getattr(exc, "limit", 0)
            raise BudgetPreemptionError(model, estimated, limit) from exc

    # C8: 헌법 활성화 → 단계적 전환
    async def check_c8_constitution_activation(
        self,
        old_version_id: str,
        new_version_id: str,
        force: bool = False,
    ) -> dict:
        """
        헌법 버전 활성화 단계적 전환.
        1) C5 충돌 감지 (force=True면 skip)
        2) 충돌 없음 → 즉시 전환
        3) non-blocking 충돌 → 단계적 전환 실행
        4) blocking 충돌 + force=False → ConstitutionConflictError
        반환: {immediately_applied, scheduled, suspended}
        """
        from engine.core.conflict_detector import ConflictDetector

        detector = ConflictDetector(self._db)
        detection_result = await detector.detect(old_version_id, new_version_id)

        if detection_result.has_blocking_conflict and not force:
            raise ConstitutionConflictError(
                conflict_count=len(detection_result.conflicts),
                blocking=True,
            )

        transition_result = await detector.apply_staged_transition(
            new_version_id, detection_result
        )
        logger.info(
            "c8_constitution_activation_complete old=%s new=%s result=%s",
            old_version_id, new_version_id,
            transition_result,
        )
        return transition_result

    # C9: 수동 재실행 (INVALID → READY, 설계자 승인)
    async def c9_manual_retry(
        self, node_id: str, actor_role: str
    ) -> bool:
        """
        설계자(SENIOR_DESIGNER 이상)만 호출 가능.
        INVALID/SUSPENDED/FAILED → READY 전환.

        재시도 시 자동으로 리셋:
        - retry_count, stall_count (기존)
        - failure_reasons (기존)
        - agent_token_usage (누적 토큰 카운터 — 이전엔 빠져있어서 재시도마다
          누적되어 node_token_limit(150K)에 조기 도달하던 버그 수정)
        - description (이전 실패 verdict 잔재 제거)

        반환: 성공 여부.
        """
        from engine.security.rbac import RBAC, Permission
        RBAC.require(actor_role, Permission.RETRY_NODE)

        node = await self._db.fetchone(
            "SELECT state, version FROM nodes WHERE id=?", (node_id,)
        )
        if not node:
            return False

        # COMPLETED → INVALID → READY (2단계 전이)
        if node["state"] == NodeState.COMPLETED:
            await self._db.execute(
                "UPDATE nodes SET state='INVALID', updated_at=datetime('now'), version=version+1 WHERE id=? AND version=?",
                (node_id, node["version"]),
            )
            node = await self._db.fetchone("SELECT state, version FROM nodes WHERE id=?", (node_id,))

        if not StateMachine.is_valid(node["state"], NodeState.READY):
            raise ValidationError(
                "INVALID_TRANSITION",
                f"C9: {node['state']} → READY 전환 불가",
            )

        # 토큰 카운터 + stall 리셋 (재시도 시 누적 초기화 — 이전엔 api/server.py
        # retry_node 엔드포인트에서만 수행돼 직접 호출 경로에서 누락되던 버그 수정)
        try:
            await self._db.execute(
                "DELETE FROM agent_token_usage WHERE node_id=?", (node_id,)
            )
        except Exception as exc:
            # 미삭제 시 토큰 누적 재사용 → 한도 조기 도달. 재시도 의미 훼손이므로
            # silent pass 대신 warning 으로 드러내 관찰 가능하게.
            logger.warning(
                "c9_retry_token_cleanup_failed node=%s error=%s", node_id, exc,
            )

        # atomic_state chunk-item 캐시 삭제 — 이 clear 가 없으면 chunked_*_items
        # 실행 시 모든 item 이 COMPLETE 로 남아있어 reserve() 가 False 반환 →
        # LLM 호출 전부 skip → 빈 skeleton 산출물 생성됨 (UI 시안 Linkwave 샘플
        # 현상의 근본 원인). task_snapshot 도 함께 초기화하여 이전 completed_items
        # 캐시가 오염된 팔레트로 재조립되는 것을 방지.
        try:
            await self._db.execute(
                "DELETE FROM atomic_state WHERE node_id=?", (node_id,)
            )
        except Exception as exc:
            # 실패 시 chunker 가 재실행 대신 skip → retry 무력화.
            # 표면에 드러내 원인 파악 가능하도록 warning.
            logger.warning(
                "c9_retry_atomic_state_cleanup_failed node=%s error=%s",
                node_id, exc,
            )
        try:
            await self._db.execute(
                "UPDATE nodes SET task_snapshot=NULL WHERE id=?", (node_id,)
            )
        except Exception as exc:
            logger.warning(
                "c9_retry_snapshot_cleanup_failed node=%s error=%s",
                node_id, exc,
            )

        affected = await self._db.execute(
            """UPDATE nodes SET state='READY', retry_count=0, stall_count=0,
               failure_reasons='[]', description=NULL,
               updated_at=datetime('now'), version=version+1
               WHERE id=? AND version=?""",
            (node_id, node["version"]),
        )
        # Re-activate outgoing edges that may have been deactivated while
        # the node was SKIPPED (e.g. by splitting.py or migration 040).
        # Without this, downstream nodes remain BLOCKED forever due to
        # missing dependency edge even after retry succeeds.
        try:
            await self._db.execute(
                "UPDATE edges SET is_active=1 WHERE from_node_id=? AND is_active=0",
                (node_id,),
            )
        except Exception:
            pass
        if affected:
            logger.info("c9_manual_retry node_id=%s actor_role=%s", node_id, actor_role)
        return affected > 0
