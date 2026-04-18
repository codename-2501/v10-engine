"""
engine/core/conflict_detector.py
ConflictDetector — C5: 헌법 버전 업그레이드 시 충돌 감지.

트리거:  새 constitution_version 활성화 시도 → ConflictDetector.detect()
처리:    1) 현재 활성 버전 vs 신규 버전의 rules_hash 비교
         2) 진행 중(IN_PROGRESS/AWAITING_APPROVAL) 노드 중
            기존 헌법 규칙 위반 가능성 탐색
         3) 충돌 목록 반환 + ConstitutionConflictDetected 이벤트 기록
         4) 충돌이 없으면 단계적 전환 스케줄(기존 노드 완료 후 신규 적용)

단계적 전환 정책 (C8 연동):
  - 신규 프로젝트: 즉시 신규 버전 적용
  - 진행 중 프로젝트: 현재 Phase 완료 후 전환
  - 충돌 발생 노드: SUSPENDED + 에스컬레이션
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)


class ConflictType(str, Enum):
    RULE_REMOVED     = "RULE_REMOVED"      # 기존 규칙이 신규 버전에서 삭제됨
    RULE_MODIFIED    = "RULE_MODIFIED"      # 핵심 규칙 변경 (의미 변경)
    ACTIVE_NODE      = "ACTIVE_NODE"        # 실행 중 노드가 영향 받음
    CONSTITUTION_GAP = "CONSTITUTION_GAP"   # 버전 간 hash 불일치 (파일 변조 의심)


@dataclass
class ConstitutionConflict:
    conflict_type: ConflictType
    description: str
    affected_node_id: str | None = None
    affected_node_name: str | None = None
    severity: str = "HIGH"   # HIGH | MEDIUM | LOW


@dataclass
class DetectionResult:
    old_version_id: str
    new_version_id: str
    conflicts: list[ConstitutionConflict] = field(default_factory=list)
    has_blocking_conflict: bool = False
    staged_transition_required: bool = False
    affected_project_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ConflictDetector
# ---------------------------------------------------------------------------

class ConflictDetector:
    """
    C5 케이스: 헌법 버전 업그레이드 충돌 감지.

    사용법:
        detector = ConflictDetector(db)
        result = await detector.detect(
            old_version_id="cv_v1_0",
            new_version_id="cv_v2_0",
        )
        if result.has_blocking_conflict:
            # 전환 차단
        elif result.staged_transition_required:
            # 단계적 전환 스케줄
        else:
            # 즉시 전환 가능
    """

    # 9대 규칙 핵심 키워드 — 이 단어가 포함된 규칙 변경은 RULE_MODIFIED
    MANDATORY_RULE_KEYWORDS = frozenset({
        "순환", "cycle", "위상", "topo", "completed",
        "mandatory", "금지", "예외 없음", "rule", "규칙",
        "qa-pair", "qa_pair", "token_budget",
    })

    def __init__(self, db: "DatabaseAdapter") -> None:
        self._db = db

    async def detect(
        self,
        old_version_id: str,
        new_version_id: str,
    ) -> DetectionResult:
        """
        헌법 버전 충돌 감지 전체 실행.
        """
        result = DetectionResult(
            old_version_id=old_version_id,
            new_version_id=new_version_id,
        )

        # 1. 두 버전 로드
        old_cv = await self._db.fetchone(
            "SELECT id, version, rules_hash, file_path FROM constitution_versions WHERE id=?",
            (old_version_id,),
        )
        new_cv = await self._db.fetchone(
            "SELECT id, version, rules_hash, file_path FROM constitution_versions WHERE id=?",
            (new_version_id,),
        )

        if not old_cv or not new_cv:
            result.conflicts.append(ConstitutionConflict(
                conflict_type=ConflictType.CONSTITUTION_GAP,
                description="헌법 버전 레코드 없음",
                severity="HIGH",
            ))
            result.has_blocking_conflict = True
            return result

        # 2. rules_hash 비교 (동일하면 충돌 없음)
        if old_cv["rules_hash"] == new_cv["rules_hash"]:
            logger.info("conflict_detector_no_change old=%s new=%s",
                        old_version_id, new_version_id)
            return result

        # 3. 파일 경로로 규칙 내용 로드 (가능하면)
        old_rules = await self._load_rules(old_cv["file_path"])
        new_rules = await self._load_rules(new_cv["file_path"])

        # 4. 규칙 차이 분석
        rule_conflicts = self._analyze_rule_diff(old_rules, new_rules)
        result.conflicts.extend(rule_conflicts)

        # 5. 진행 중 노드 조회 (ACTIVE 상태)
        active_nodes = await self._db.fetchall(
            """SELECT n.id, n.name, n.state, n.phase, n.project_id,
                      n.constitution_version_id
               FROM nodes n
               WHERE n.state IN ('IN_PROGRESS','AWAITING_APPROVAL','NEEDS_HUMAN')
                 AND (n.constitution_version_id=? OR n.constitution_version_id IS NULL)""",
            (old_version_id,),
        )

        # 6. 활성 노드별 충돌 평가
        affected_projects: set[str] = set()
        for node in active_nodes:
            node_conflict = self._assess_node_conflict(node, rule_conflicts)
            if node_conflict:
                result.conflicts.append(node_conflict)
                affected_projects.add(node["project_id"])

        result.affected_project_ids = list(affected_projects)

        # 7. 단계적 전환 여부 결정
        has_active_conflicts = any(
            c.conflict_type == ConflictType.ACTIVE_NODE for c in result.conflicts
        )
        has_mandatory_change = any(
            c.conflict_type in (ConflictType.RULE_REMOVED, ConflictType.RULE_MODIFIED)
            and c.severity == "HIGH"
            for c in result.conflicts
        )

        result.has_blocking_conflict = has_mandatory_change and has_active_conflicts
        result.staged_transition_required = has_active_conflicts or bool(affected_projects)

        # 8. 이벤트 기록
        if result.conflicts:
            await self._emit_event(
                "ConstitutionConflictDetected",
                {
                    "old_version_id": old_version_id,
                    "new_version_id": new_version_id,
                    "conflict_count": len(result.conflicts),
                    "has_blocking": result.has_blocking_conflict,
                    "staged_required": result.staged_transition_required,
                    "affected_projects": len(affected_projects),
                },
            )

        logger.info(
            "conflict_detection_complete old=%s new=%s conflicts=%s blocking=%s staged=%s",
            old_version_id, new_version_id,
            len(result.conflicts),
            result.has_blocking_conflict,
            result.staged_transition_required,
        )
        return result

    async def apply_staged_transition(
        self,
        new_version_id: str,
        detection_result: DetectionResult,
    ) -> dict:
        """
        C8 단계적 전환 실행:
        - 신규 프로젝트: 즉시 새 버전 적용
        - 진행 중 프로젝트: Phase 완료 후 전환 예약
        - 충돌 노드: SUSPENDED + escalation
        Returns: {"immediately_applied": int, "scheduled": int, "suspended": int}
        """
        immediately_applied = 0
        scheduled = 0
        suspended = 0

        # 신규 버전을 is_active=1 로 설정
        await self._db.execute(
            "UPDATE constitution_versions SET is_active=0 WHERE is_active=1"
        )
        await self._db.execute(
            "UPDATE constitution_versions SET is_active=1, activated_at=datetime('now') WHERE id=?",
            (new_version_id,),
        )

        # 진행 중 충돌 노드 → SUSPENDED
        conflict_node_ids = [
            c.affected_node_id for c in detection_result.conflicts
            if c.conflict_type == ConflictType.ACTIVE_NODE and c.affected_node_id
        ]
        for node_id in conflict_node_ids:
            affected = await self._db.execute(
                """UPDATE nodes
                   SET state='SUSPENDED', suspension_reason='CONSTITUTION_UPGRADE',
                       updated_at=datetime('now'), version=version+1
                   WHERE id=? AND state IN ('IN_PROGRESS','AWAITING_APPROVAL')""",
                (node_id,),
            )
            if affected:
                suspended += 1
                await self._create_escalation(node_id, new_version_id)

        # 영향 받지 않는 프로젝트: 즉시 새 버전 적용
        all_project_ids = {
            r["id"] for r in await self._db.fetchall(
                "SELECT id FROM projects WHERE status IN ('INTAKE','ACTIVE')"
            )
        }
        conflict_projects = set(detection_result.affected_project_ids)
        safe_projects = all_project_ids - conflict_projects

        for pid in safe_projects:
            await self._db.execute(
                "UPDATE projects SET constitution_version_id=? WHERE id=?",
                (new_version_id, pid),
            )
            immediately_applied += 1

        # 충돌 프로젝트: 스케줄 마킹 (현재 Phase 완료 후 적용)
        scheduled = len(conflict_projects)

        await self._emit_event(
            "ConstitutionActivated",
            {
                "version_id": new_version_id,
                "immediately_applied": immediately_applied,
                "scheduled": scheduled,
                "suspended": suspended,
            },
        )

        logger.info(
            "constitution_staged_transition_complete version=%s immediately=%s scheduled=%s suspended=%s",
            new_version_id,
            immediately_applied,
            scheduled,
            suspended,
        )
        return {
            "immediately_applied": immediately_applied,
            "scheduled": scheduled,
            "suspended": suspended,
        }

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    async def _load_rules(self, file_path: str) -> str:
        """헌법 파일 내용 로드 (없으면 빈 문자열)."""
        try:
            with open(file_path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _analyze_rule_diff(
        self, old_rules: str, new_rules: str
    ) -> list[ConstitutionConflict]:
        """규칙 텍스트 차이 분석."""
        conflicts: list[ConstitutionConflict] = []
        if not old_rules or not new_rules:
            return conflicts

        old_lines = set(old_rules.splitlines())
        new_lines = set(new_rules.splitlines())

        removed = old_lines - new_lines
        # added = new_lines - old_lines  # 추가된 규칙은 충돌 없음

        for line in removed:
            line_lower = line.lower()
            has_keyword = any(kw in line_lower for kw in self.MANDATORY_RULE_KEYWORDS)
            if has_keyword and len(line.strip()) > 5:
                conflicts.append(ConstitutionConflict(
                    conflict_type=ConflictType.RULE_MODIFIED,
                    description=f"MANDATORY 규칙 변경 감지: {line.strip()[:80]}",
                    severity="HIGH",
                ))
            elif len(line.strip()) > 10:
                conflicts.append(ConstitutionConflict(
                    conflict_type=ConflictType.RULE_REMOVED,
                    description=f"규칙 삭제: {line.strip()[:80]}",
                    severity="MEDIUM",
                ))

        return conflicts

    def _assess_node_conflict(
        self,
        node: dict,
        rule_conflicts: list[ConstitutionConflict],
    ) -> ConstitutionConflict | None:
        """활성 노드와 규칙 충돌 결합 평가."""
        if not rule_conflicts:
            return None
        high_conflicts = [c for c in rule_conflicts if c.severity == "HIGH"]
        if not high_conflicts:
            return None
        return ConstitutionConflict(
            conflict_type=ConflictType.ACTIVE_NODE,
            description=(
                f"노드 '{node['name']}' ({node['state']})이 "
                f"현재 버전 헌법으로 실행 중, 업그레이드 시 충돌 가능"
            ),
            affected_node_id=node["id"],
            affected_node_name=node["name"],
            severity="HIGH",
        )

    async def _create_escalation(self, node_id: str, version_id: str) -> None:
        """충돌 노드에 에스컬레이션 생성."""
        node = await self._db.fetchone(
            "SELECT project_id, name FROM nodes WHERE id=?", (node_id,)
        )
        if not node:
            return
        esc_id = str(uuid.uuid4())
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                """INSERT INTO escalations
                   (id, project_id, node_id, escalation_type, severity,
                    title, description, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'CONSTITUTION', 'HIGH',
                           '헌법 버전 업그레이드 충돌', ?, 'OPEN', ?, ?)""",
                (
                    esc_id, node["project_id"], node_id,
                    f"노드 '{node['name']}': 헌법 {version_id} 업그레이드로 SUSPENDED",
                    now, now,
                ),
            )
        except Exception as exc:
            logger.warning("conflict_escalation_insert_failed error=%s", str(exc))

    async def _emit_event(self, event_type: str, payload: dict) -> None:
        event_id = str(uuid.uuid4())
        checksum = hashlib.sha256(
            f"{event_id}{event_type}{json.dumps(payload, sort_keys=True)}".encode()
        ).hexdigest()
        try:
            await self._db.execute(
                """INSERT INTO event_store
                   (event_id, aggregate_type, aggregate_id, event_type,
                    payload, checksum, recorded_at)
                   VALUES (?, 'Constitution', 'constitution', ?, ?, ?, datetime('now'))""",
                (event_id, event_type, json.dumps(payload), checksum),
            )
        except Exception as exc:
            logger.warning("conflict_event_insert_failed error=%s", str(exc))
