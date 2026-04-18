"""
Stage 4: Coverage Verifier — chunk 노드의 expected_items vs produced_items 대조.

누락 item 발견 시 retry_missing() 으로 해당 item 만 재생성. 3회 실패 후에도
남으면 NEEDS_HUMAN 전이 → Stage 17 Human review UI 에서 개입.

사용 패턴 (executor.py 의 chunk 노드 실행 끝에서):

    verifier = CoverageVerifier(db, state_store, model_adapter, assembler)
    report = await verifier.verify(engagement_id, node, expected_items)
    if report.missing:
        await verifier.retry_missing(engagement_id, node, report, spec, assembly)
    await verifier.save_report(engagement_id, node, report)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 노드 기준 자동 재시도 최대 횟수. 이 값을 넘으면 NEEDS_HUMAN.
DEFAULT_MAX_RETRIES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CoverageReport:
    expected_count: int
    produced_count: int
    missing: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    retry_attempts: int = 0

    @property
    def coverage_ratio(self) -> float:
        if self.expected_count == 0:
            return 1.0
        return self.produced_count / self.expected_count

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.needs_human


class CoverageVerifier:
    """chunk 노드 커버리지 검증 + 누락 재큐잉."""

    def __init__(
        self,
        db: Any,
        state_store: Any,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._db = db
        self._state = state_store
        self._max_retries = max_retries

    async def verify(
        self,
        engagement_id: str,
        node_id: str,
        expected: list[str],
    ) -> CoverageReport:
        """expected vs produced 대조 리포트 생성."""
        if not expected:
            return CoverageReport(expected_count=0, produced_count=0)

        # status 별 집계
        rows = await self._db.fetchall(
            """SELECT item_key, status FROM atomic_state
               WHERE engagement_id=? AND node_id=?""",
            (engagement_id, node_id),
        )
        status_map = {r["item_key"]: r["status"] for r in rows}

        complete: list[str] = []
        missing: list[str] = []
        needs_human: list[str] = []
        skipped: list[str] = []

        for item in expected:
            st = status_map.get(item, "PENDING")
            if st == "COMPLETE":
                complete.append(item)
            elif st == "SKIPPED":
                skipped.append(item)  # 운영자가 명시적 포기
            elif st == "NEEDS_HUMAN":
                needs_human.append(item)
            else:  # PENDING / RESERVED(진행중 or stuck) / FAILED
                missing.append(item)

        return CoverageReport(
            expected_count=len(expected),
            produced_count=len(complete) + len(skipped),
            missing=missing,
            needs_human=needs_human,
            skipped=skipped,
        )

    async def retry_missing(
        self,
        engagement_id: str,
        node_id: str,
        missing: list[str],
        regenerate_fn,
    ) -> list[str]:
        """누락 item 재실행. regenerate_fn(item_key) -> artifact_str|None.

        - regenerate_fn 성공 시 state_store.complete
        - 실패 시 state_store.fail (retry_count 누적)
        - retry_count > max_retries 면 NEEDS_HUMAN 전이

        반환: 재시도 후에도 실패한 item 목록 (NEEDS_HUMAN 포함).
        """
        still_missing: list[str] = []
        for item in missing:
            rc = await self._state.get_retry_count(engagement_id, node_id, item)
            if rc >= self._max_retries:
                await self._state.mark_needs_human(
                    engagement_id, node_id, item,
                    f"max_retries={self._max_retries} exceeded",
                )
                still_missing.append(item)
                logger.warning(
                    "coverage_needs_human node=%s item=%s retries=%d",
                    node_id[:8], item, rc,
                )
                continue

            reserved = await self._state.reserve(engagement_id, node_id, item)
            if not reserved:
                # 다른 워커가 선점했거나 이미 COMPLETE — skip
                continue

            try:
                artifact = await regenerate_fn(item)
                if artifact:
                    # content hash 는 감사·재사용 용도
                    import hashlib
                    h = hashlib.sha256(
                        str(artifact).encode("utf-8")
                    ).hexdigest()[:16]
                    await self._state.complete(
                        engagement_id, node_id, item, h,
                    )
                    logger.info(
                        "coverage_retry_success node=%s item=%s",
                        node_id[:8], item,
                    )
                else:
                    await self._state.fail(
                        engagement_id, node_id, item, "regenerate_fn returned empty",
                    )
                    still_missing.append(item)
            except Exception as e:
                await self._state.fail(
                    engagement_id, node_id, item, str(e)[:300],
                )
                still_missing.append(item)
                logger.warning(
                    "coverage_retry_fail node=%s item=%s err=%s",
                    node_id[:8], item, str(e)[:120],
                )

        return still_missing

    async def save_report(
        self,
        engagement_id: str,
        node_id: str,
        report: CoverageReport,
    ) -> None:
        """coverage_report 테이블에 기록."""
        await self._db.execute(
            """INSERT INTO coverage_report
                 (engagement_id, node_id, expected_count, produced_count,
                  missing_items, retry_attempts, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(engagement_id, node_id) DO UPDATE SET
                 expected_count=excluded.expected_count,
                 produced_count=excluded.produced_count,
                 missing_items=excluded.missing_items,
                 retry_attempts=excluded.retry_attempts,
                 verified_at=excluded.verified_at""",
            (
                engagement_id, node_id,
                report.expected_count, report.produced_count,
                json.dumps(
                    report.missing + report.needs_human,
                    ensure_ascii=False,
                ),
                report.retry_attempts,
                _now(),
            ),
        )

    async def get_report(
        self, engagement_id: str, node_id: str,
    ) -> CoverageReport | None:
        """최신 저장된 리포트 조회 — Flow 뷰 사이드패널용."""
        row = await self._db.fetchone(
            """SELECT expected_count, produced_count, missing_items, retry_attempts
               FROM coverage_report
               WHERE engagement_id=? AND node_id=?""",
            (engagement_id, node_id),
        )
        if not row:
            return None
        try:
            missing_raw = json.loads(row["missing_items"] or "[]")
        except Exception:
            missing_raw = []
        return CoverageReport(
            expected_count=int(row["expected_count"] or 0),
            produced_count=int(row["produced_count"] or 0),
            missing=list(missing_raw),
            retry_attempts=int(row["retry_attempts"] or 0),
        )
