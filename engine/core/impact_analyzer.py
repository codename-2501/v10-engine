"""
engine/core/impact_analyzer.py
ImpactAnalyzer — C4: 요구사항 대규모 변경 시 영향 분석 배치 실행.

트리거:  요구사항 content_hash 변경 감지 → ImpactAnalyzer.analyze()
처리:    1) 변경된 요구사항에 의존하는 downstream 노드 식별
         2) 각 노드의 artifact_versions와 deltas를 비교해 영향 심각도 계산
         3) CRITICAL/HIGH 노드는 Cascade Invalidation Phase1 예약
         4) 이벤트 기록 (ImpactAnalysisStarted / ImpactNodesFound)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.db.adapter import DatabaseAdapter

logger = logging.getLogger(__name__)


class ImpactSeverity(str, Enum):
    CRITICAL = "CRITICAL"   # 노드 자체가 요구사항을 직접 구현
    HIGH     = "HIGH"       # 요구사항 기반 산출물 존재
    MEDIUM   = "MEDIUM"     # 간접 의존 (2단계 이상 downstream)
    LOW      = "LOW"        # 참조만 있음, 재실행 불필요


@dataclass
class ImpactedNode:
    node_id: str
    node_name: str
    state: str
    phase: str
    distance: int          # 변경 요구사항에서의 그래프 거리
    severity: ImpactSeverity
    artifact_count: int = 0
    should_invalidate: bool = False


@dataclass
class ImpactAnalysisResult:
    requirement_id: str
    old_version: int
    new_version: int
    change_summary: str
    impacted_nodes: list[ImpactedNode] = field(default_factory=list)
    total_critical: int = 0
    total_high: int = 0
    total_medium: int = 0
    total_low: int = 0
    invalidation_scheduled: int = 0


# ---------------------------------------------------------------------------
# ImpactAnalyzer
# ---------------------------------------------------------------------------

class ImpactAnalyzer:
    """
    C4 케이스: 요구사항 대규모 변경 → 영향 받는 노드 분석 + Cascade 예약.

    사용법:
        analyzer = ImpactAnalyzer(db)
        result = await analyzer.analyze(
            requirement_id="req-123",
            old_version=2,
            new_version=3,
            change_summary="DB 스키마 전면 변경"
        )
    """

    # 변경 심각도 임계값: 이 이상이면 CRITICAL (0~1)
    CRITICAL_CHANGE_THRESHOLD = 0.5
    # BFS 최대 탐색 깊이
    MAX_BFS_DEPTH = 10

    def __init__(self, db: "DatabaseAdapter") -> None:
        self._db = db

    async def analyze(
        self,
        requirement_id: str,
        old_version: int,
        new_version: int,
        change_summary: str = "",
    ) -> ImpactAnalysisResult:
        """
        요구사항 변경 → 영향 분석 전체 실행.
        1) 변경 심각도 계산
        2) BFS로 downstream 노드 수집
        3) 각 노드 영향 평가
        4) CRITICAL/HIGH → Cascade Invalidation Phase1 예약
        5) 이벤트 기록
        """
        result = ImpactAnalysisResult(
            requirement_id=requirement_id,
            old_version=old_version,
            new_version=new_version,
            change_summary=change_summary,
        )

        # 이벤트: ImpactAnalysisStarted
        await self._emit_event(
            "ImpactAnalysisStarted",
            {"requirement_id": requirement_id,
             "old_version": old_version, "new_version": new_version},
        )

        # 1. 요구사항이 속한 프로젝트 조회
        req_row = await self._db.fetchone(
            "SELECT project_id FROM requirements WHERE id=?", (requirement_id,)
        )
        if not req_row:
            logger.warning("impact_analyzer_req_not_found requirement_id=%s", requirement_id)
            return result

        project_id = req_row["project_id"]

        # 2. 프로젝트 노드 전체 + 엣지 로드 (BFS 준비)
        nodes = await self._db.fetchall(
            """SELECT n.id, n.name, n.state, n.phase, n.dag_id
               FROM nodes n
               JOIN dags d ON d.id = n.dag_id
               WHERE d.project_id=?""",
            (project_id,),
        )
        node_map = {n["id"]: dict(n) for n in nodes}

        edges = await self._db.fetchall(
            """SELECT e.from_node_id, e.to_node_id
               FROM edges e
               JOIN dags d ON d.id = e.dag_id
               WHERE d.project_id=? AND e.is_active=1""",
            (project_id,),
        )

        # 요구사항을 "직접 소유"하는 노드 찾기 (DEFINE phase 노드)
        root_nodes = [
            n["id"] for n in nodes
            if n["phase"] in ("DEFINE",)
        ]

        # 3. BFS로 downstream 노드 거리 계산
        distances = self._bfs_distances(root_nodes, edges)

        # 4. 각 노드의 artifact 수 조회
        artifact_counts = await self._count_artifacts_per_node(project_id)

        # 5. 변경 심각도 계산 (단순: content 유사도)
        change_ratio = await self._calculate_change_ratio(
            requirement_id, old_version, new_version
        )

        # 6. 노드별 영향 평가
        for node_id, node in node_map.items():
            distance = distances.get(node_id, 999)
            if distance > self.MAX_BFS_DEPTH:
                continue

            severity = self._calculate_severity(
                distance, change_ratio, artifact_counts.get(node_id, 0)
            )
            should_invalidate = severity in (ImpactSeverity.CRITICAL, ImpactSeverity.HIGH)

            impacted = ImpactedNode(
                node_id=node_id,
                node_name=node["name"],
                state=node["state"],
                phase=node["phase"],
                distance=distance,
                severity=severity,
                artifact_count=artifact_counts.get(node_id, 0),
                should_invalidate=should_invalidate,
            )
            result.impacted_nodes.append(impacted)

            # 집계
            if severity == ImpactSeverity.CRITICAL:
                result.total_critical += 1
            elif severity == ImpactSeverity.HIGH:
                result.total_high += 1
            elif severity == ImpactSeverity.MEDIUM:
                result.total_medium += 1
            else:
                result.total_low += 1

        # 7. Cascade Invalidation Phase1 예약 (CRITICAL + HIGH)
        for node in result.impacted_nodes:
            if node.should_invalidate and node.state not in ("COMPLETED_ALREADY_INVALID",):
                await self._schedule_cascade(
                    node.node_id, requirement_id, change_summary
                )
                result.invalidation_scheduled += 1

        # 이벤트: ImpactNodesFound
        await self._emit_event(
            "ImpactNodesFound",
            {
                "requirement_id": requirement_id,
                "total_impacted": len(result.impacted_nodes),
                "critical": result.total_critical,
                "high": result.total_high,
                "invalidation_scheduled": result.invalidation_scheduled,
            },
        )

        logger.info(
            "impact_analysis_complete requirement_id=%s total=%s critical=%s high=%s invalidation_scheduled=%s",
            requirement_id,
            len(result.impacted_nodes),
            result.total_critical,
            result.total_high,
            result.invalidation_scheduled,
        )
        return result

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _bfs_distances(
        self,
        root_ids: list[str],
        edges: list[dict],
    ) -> dict[str, int]:
        """root_ids에서 시작하는 BFS → {node_id: distance}."""
        adjacency: dict[str, list[str]] = {}
        for e in edges:
            adjacency.setdefault(e["from_node_id"], []).append(e["to_node_id"])

        distances: dict[str, int] = {}
        queue: list[tuple[str, int]] = [(nid, 0) for nid in root_ids]
        for nid, d in queue:
            distances[nid] = d

        i = 0
        while i < len(queue):
            cur_id, cur_dist = queue[i]
            i += 1
            if cur_dist >= self.MAX_BFS_DEPTH:
                continue
            for neighbor in adjacency.get(cur_id, []):
                if neighbor not in distances:
                    distances[neighbor] = cur_dist + 1
                    queue.append((neighbor, cur_dist + 1))
        return distances

    async def _count_artifacts_per_node(self, project_id: str) -> dict[str, int]:
        """노드별 artifact 수."""
        rows = await self._db.fetchall(
            "SELECT node_id, COUNT(*) AS cnt FROM artifacts WHERE project_id=? GROUP BY node_id",
            (project_id,),
        )
        return {r["node_id"]: r["cnt"] for r in rows}

    async def _calculate_change_ratio(
        self, requirement_id: str, old_version: int, new_version: int
    ) -> float:
        """
        두 버전의 content_hash 차이로 변경 비율 계산.
        0.0 = 동일, 1.0 = 완전히 다름.
        """
        rows = await self._db.fetchall(
            """SELECT version_num, content_hash, content
               FROM requirement_versions
               WHERE requirement_id=? AND version_num IN (?, ?)
               ORDER BY version_num""",
            (requirement_id, old_version, new_version),
        )
        if len(rows) < 2:
            return 0.8  # 비교 불가 시 높은 값으로 보수적 처리

        old_row = next((r for r in rows if r["version_num"] == old_version), None)
        new_row = next((r for r in rows if r["version_num"] == new_version), None)
        if not old_row or not new_row:
            return 0.8

        if old_row["content_hash"] == new_row["content_hash"]:
            return 0.0

        # 간단한 단어 기반 유사도 (Jaccard)
        old_words = set(old_row["content"].lower().split())
        new_words = set(new_row["content"].lower().split())
        if not old_words and not new_words:
            return 0.0
        intersection = len(old_words & new_words)
        union = len(old_words | new_words)
        similarity = intersection / union if union else 0
        return 1.0 - similarity

    def _calculate_severity(
        self, distance: int, change_ratio: float, artifact_count: int
    ) -> ImpactSeverity:
        """
        거리 + 변경비율 + 산출물 수 → 심각도 계산.
        """
        if distance == 0 and change_ratio >= self.CRITICAL_CHANGE_THRESHOLD:
            return ImpactSeverity.CRITICAL
        if distance <= 1 and (change_ratio >= 0.3 or artifact_count > 0):
            return ImpactSeverity.HIGH
        if distance <= 3:
            return ImpactSeverity.MEDIUM
        return ImpactSeverity.LOW

    async def _schedule_cascade(
        self,
        node_id: str,
        requirement_id: str,
        reason: str,
        change_type: str = "CONTEXTUAL",
        affected_sections: list | None = None,
    ) -> None:
        """Cascade Invalidation Phase1: invalidation_pending 마킹."""
        import json as _json
        sections_json = _json.dumps(affected_sections or [], ensure_ascii=False)
        await self._db.execute(
            """UPDATE nodes
               SET invalidation_pending=1,
                   invalidation_source_id=?,
                   invalidation_queued_at=datetime('now'),
                   invalidation_change_type=?,
                   invalidation_affected_sections=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (requirement_id, change_type, sections_json, node_id),
        )
        logger.debug(
            "impact_cascade_scheduled node_id=%s source=%s change_type=%s",
            node_id, requirement_id, change_type,
        )

    async def _emit_event(self, event_type: str, payload: dict) -> None:
        import uuid
        event_id = str(uuid.uuid4())
        checksum = hashlib.sha256(
            f"{event_id}{event_type}{json.dumps(payload, sort_keys=True)}".encode()
        ).hexdigest()
        try:
            await self._db.execute(
                """INSERT INTO event_store
                   (event_id, aggregate_type, aggregate_id, event_type,
                    payload, checksum, recorded_at)
                   VALUES (?, 'Requirement', ?, ?, ?, ?, datetime('now'))""",
                (event_id, payload.get("requirement_id", ""), event_type,
                 json.dumps(payload), checksum),
            )
        except Exception as exc:
            logger.warning("impact_event_insert_failed error=%s", str(exc))
