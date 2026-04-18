"""
Stage 5 + Stage 18: Global Context Advisor + Circuit Breaker.

chunk 간 일관성 검증 (글꼴·톤·스키마 드리프트 방지). Haiku 같은 작은 모델에
위임해 비용 최소화. 연속 reject 발생 시 circuit breaker 로 무한 루프 방지.

사용 패턴 (executor.py chunk 루프 내):

    advisor = GlobalAdvisor(db, model_adapter)
    result = await advisor.review_chunk(eng_id, node, item_key, artifact)
    if result.inconsistent:
        # 해당 chunk 만 재생성 (최대 2회)
        ...

Feature flag: V8_ADVISOR=0 → 완전 bypass (review_chunk always ACCEPT).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_ADVISOR", "1") != "0"
_MODEL = os.environ.get("V8_ADVISOR_MODEL", "claude-haiku-4-5-20251001")
_REJECT_THRESHOLD = int(os.environ.get("V8_ADVISOR_REJECT_THRESHOLD", "5"))
_COOLDOWN_S = int(os.environ.get("V8_ADVISOR_COOLDOWN_S", "300"))
_MAX_TOKENS = int(os.environ.get("V8_ADVISOR_MAX_TOKENS", "800"))


@dataclass
class ReviewResult:
    accept: bool
    reason: str = ""
    severity: str = "info"  # info | warn | error
    skipped: bool = False   # circuit breaker 에 의해 스킵됨

    @property
    def inconsistent(self) -> bool:
        return not self.accept and not self.skipped


# ---------------------------------------------------------------------------
# Stage 18: Circuit Breaker
# ---------------------------------------------------------------------------

class AdvisorCircuitBreaker:
    """연속 reject 임계 초과 시 Advisor 일시 비활성화 (무한 루프 방어)."""

    def __init__(
        self, threshold: int = _REJECT_THRESHOLD,
        cooldown_s: int = _COOLDOWN_S,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._reject_streak: dict[str, int] = defaultdict(int)
        self._paused_until: dict[str, float] = {}

    def _key(self, engagement_id: str, node_type: str) -> str:
        return f"{engagement_id}:{node_type}"

    def record_reject(self, engagement_id: str, node_type: str) -> None:
        k = self._key(engagement_id, node_type)
        self._reject_streak[k] += 1
        if self._reject_streak[k] >= self._threshold:
            self._paused_until[k] = time.time() + self._cooldown_s
            logger.warning(
                "advisor_circuit_breaker_tripped key=%s streak=%d cooldown=%ds",
                k, self._reject_streak[k], self._cooldown_s,
            )

    def record_accept(self, engagement_id: str, node_type: str) -> None:
        k = self._key(engagement_id, node_type)
        self._reject_streak[k] = 0

    def should_review(self, engagement_id: str, node_type: str) -> bool:
        k = self._key(engagement_id, node_type)
        paused_until = self._paused_until.get(k, 0)
        if time.time() < paused_until:
            return False
        # cooldown 경과 시 streak 리셋
        if paused_until and time.time() >= paused_until:
            self._reject_streak[k] = 0
            self._paused_until.pop(k, None)
            logger.info("advisor_circuit_recovered key=%s", k)
        return True

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "active_breakers": [
                {"key": k, "cooldown_remaining_s": max(0, int(v - now))}
                for k, v in self._paused_until.items() if v > now
            ],
            "reject_streaks": dict(self._reject_streak),
        }


# ---------------------------------------------------------------------------
# Global Advisor
# ---------------------------------------------------------------------------

class GlobalAdvisor:
    """chunk 산출물을 PRD·디자인시스템·이전 결정사항 스냅샷과 대조 검증."""

    def __init__(
        self,
        db: Any,
        model_adapter: Any,
        enabled: bool | None = None,
    ) -> None:
        self._db = db
        self._model = model_adapter
        self._enabled = _ENABLED if enabled is None else enabled
        self._breaker = AdvisorCircuitBreaker()
        # engagement 별 snapshot 캐시 (chunk 마다 재조립 비용 절감)
        self._snapshot_cache: dict[str, tuple[float, dict]] = {}
        self._snapshot_ttl_s = 60  # 1분
        self._lock = asyncio.Lock()

    async def snapshot(self, engagement_id: str) -> dict:
        """현재 engagement 의 결정사항 스냅샷.

        최소 정보만 담아 Haiku 에게 review context 로 전달.
        - 디자인 토큰 요약 (색·폰트·간격)
        - API 스키마 요약 (엔드포인트 목록만)
        - DB 스키마 요약 (테이블 이름만)
        """
        async with self._lock:
            cached = self._snapshot_cache.get(engagement_id)
            if cached and time.time() - cached[0] < self._snapshot_ttl_s:
                return cached[1]

        snap: dict = {"engagement_id": engagement_id}
        try:
            # 디자인 토큰 artifact 요약
            row = await self._db.fetchone(
                """SELECT av.storage_path AS content FROM nodes n
                   JOIN artifacts a ON a.node_id=n.id
                   JOIN artifact_versions av
                     ON av.artifact_id=a.id AND av.version_num=a.current_version
                   WHERE n.name LIKE '%디자인 토큰%' OR n.name LIKE '%design token%'
                   ORDER BY n.updated_at DESC LIMIT 1""",
            )
            if row and row.get("content"):
                try:
                    tokens = json.loads(row["content"])
                    snap["design_tokens"] = {
                        "colors": list((tokens.get("colors") or {}).keys())[:20],
                        "typography_keys": list((tokens.get("typography") or {}).keys())[:10],
                    }
                except Exception:
                    snap["design_tokens_raw_head"] = row["content"][:500]
        except Exception as e:
            logger.debug("advisor_snapshot_design_fail %s", e)

        async with self._lock:
            self._snapshot_cache[engagement_id] = (time.time(), snap)
        return snap

    def invalidate_snapshot(self, engagement_id: str) -> None:
        self._snapshot_cache.pop(engagement_id, None)

    async def review_chunk(
        self,
        engagement_id: str,
        node_id: str,
        node_type: str,
        item_key: str,
        artifact_head: str,
    ) -> ReviewResult:
        """chunk 산출물이 스냅샷과 일치하는지 Haiku 로 판정.

        artifact_head: artifact 의 앞부분 (권장 3~5K chars). 전체를 보내지 않음.
        """
        if not self._enabled:
            return ReviewResult(accept=True, reason="disabled", skipped=True)

        # Circuit breaker 체크
        if not self._breaker.should_review(engagement_id, node_type):
            return ReviewResult(
                accept=True, reason="circuit_breaker_open", skipped=True,
            )

        snap = await self.snapshot(engagement_id)
        prompt = self._build_review_prompt(snap, node_type, item_key, artifact_head)

        try:
            resp = await self._model.call(
                model=_MODEL,
                system="당신은 산출물 일관성 검증자입니다. JSON 으로만 답변하세요.",
                prompt=prompt,
                max_tokens=_MAX_TOKENS,
                temperature=0.0,
            )
            return self._parse_response(resp.content, engagement_id, node_type)
        except Exception as e:
            logger.warning("advisor_call_fail node=%s item=%s err=%s",
                           node_id[:8], item_key, str(e)[:120])
            # Advisor 실패는 산출물 실패와 무관 — 통과 처리
            return ReviewResult(accept=True, reason=f"advisor_error:{e!s:.80}", skipped=True)

    def _build_review_prompt(
        self, snap: dict, node_type: str, item_key: str, artifact_head: str,
    ) -> str:
        return (
            "다음 산출물이 프로젝트 공통 규약과 일치하는지 판정하세요.\n\n"
            f"[공통 규약 스냅샷]\n{json.dumps(snap, ensure_ascii=False)[:1500]}\n\n"
            f"[산출물 정보]\n- 종류: {node_type}\n- 항목: {item_key}\n"
            f"- 내용 앞부분:\n{artifact_head[:3500]}\n\n"
            "판정 기준:\n"
            "- 디자인 토큰 색상/폰트를 사용하는가 (var(--...) 형태)\n"
            "- 하드코딩 색상(#abc 직접 명시)이 과도하지 않은가\n"
            "- item_key 패턴이 산출물에 반영돼 있는가 (id=... 등)\n\n"
            "응답 형식(JSON only):\n"
            '{\n  "accept": true|false,\n  "reason": "한 줄 사유",\n  '
            '"severity": "info|warn|error"\n}'
        )

    def _parse_response(
        self, content: str, engagement_id: str, node_type: str,
    ) -> ReviewResult:
        try:
            # Haiku 가 앞뒤에 텍스트 붙일 수 있음 — 첫 { ~ 마지막 } 추출
            s = content.find("{")
            e = content.rfind("}")
            if s == -1 or e == -1 or e < s:
                raise ValueError("no-json-object")
            data = json.loads(content[s:e + 1])
            accept = bool(data.get("accept", True))
            reason = str(data.get("reason", ""))[:200]
            severity = data.get("severity", "info")

            if accept:
                self._breaker.record_accept(engagement_id, node_type)
            else:
                self._breaker.record_reject(engagement_id, node_type)

            return ReviewResult(
                accept=accept, reason=reason, severity=severity,
            )
        except Exception as e:
            logger.debug("advisor_parse_fail %s content=%.120s", e, content)
            # 파싱 실패는 통과 처리 (엄격 reject 피함)
            return ReviewResult(
                accept=True, reason=f"parse_fail:{e!s:.60}", skipped=True,
            )

    def circuit_snapshot(self) -> dict:
        return self._breaker.snapshot()

    # ---------------------------------------------------------------------
    # Stage 22-B: Semantic Checklist Review
    # ---------------------------------------------------------------------

    async def review_semantic(
        self,
        engagement_id: str,
        node_id: str,
        node_type: str,
        item_key: str,
        artifact_head: str,
        semantic_checklist: list[str],
    ) -> ReviewResult:
        """spec.semantic_checklist 각 항목을 Haiku 에게 Y/N 판정 요청.

        기본 review_chunk 는 "디자인 토큰·클래스 쪽 일관성" 위주.
        review_semantic 은 "의미 기능 반영" 초점 — 예:
            - 요양보호사 대시보드에 관리자 전용 요소가 없는가
            - 로그인 페이지에 실제 로그인 폼이 있는가
            - CTA 버튼이 맥락에 맞는 동작을 수행하는가

        Circuit Breaker 공유 (review_chunk 와 동일 key).
        V8_SEMANTIC_ADVISOR=0 → bypass.
        """
        import os as _os
        if _os.environ.get("V8_SEMANTIC_ADVISOR", "1") == "0":
            return ReviewResult(accept=True, reason="disabled", skipped=True)
        if not self._enabled or not semantic_checklist:
            return ReviewResult(accept=True, reason="empty_checklist", skipped=True)

        if not self._breaker.should_review(engagement_id, f"semantic:{node_type}"):
            return ReviewResult(
                accept=True, reason="circuit_breaker_open", skipped=True,
            )

        prompt = (
            "다음 산출물이 아래 체크리스트를 모두 만족하는지 판정하세요.\n\n"
            f"[체크리스트]\n- " + "\n- ".join(semantic_checklist) + "\n\n"
            f"[산출물 정보]\n- 종류: {node_type}\n- 항목: {item_key}\n"
            f"- 내용:\n{artifact_head[:3500]}\n\n"
            "모든 체크리스트 항목에 대해 Y/N 판정 후 JSON 으로:\n"
            '{\n  "accept": true|false,\n  "missing_items": [위반 항목...],\n'
            '  "reason": "한 줄 사유"\n}'
        )
        try:
            resp = await self._model.call(
                model=_MODEL,
                system="당신은 산출물이 기능 요구사항을 실제로 반영하는지 판정하는 의미 검증자입니다. JSON 으로만 답변.",
                prompt=prompt,
                max_tokens=_MAX_TOKENS,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning(
                "advisor_semantic_call_fail node=%s item=%s err=%s",
                node_id[:8], item_key, str(e)[:120],
            )
            return ReviewResult(accept=True, reason=f"advisor_error", skipped=True)

        result = self._parse_response(resp.content, engagement_id, f"semantic:{node_type}")
        if result.inconsistent:
            logger.warning(
                "advisor_semantic_reject node=%s item=%s reason=%s",
                node_id[:8], item_key, result.reason[:100],
            )
        return result
