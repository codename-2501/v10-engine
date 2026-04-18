"""
Stage 26: PRD Clarification Loop.

PRD 내 모호 표현 자동 감지 + 사용자 질문 생성 + 답변 merge.
refined PRD 로 DAG 실행 → "엉뚱한 산출물" 예방.

사용 흐름 (intake pipeline 에서):
    clar = PRDClarifier(db, model_adapter)
    ambiguities = await clar.scan_ambiguities(prd_text)  # 코드 기반
    if ambiguities:
        questions = clar.to_questions(ambiguities)
        await clar.save_questions(engagement_id, questions)
        # 사용자 답변 완료 대기 (UI 에서 POST 받음)
        ...
        refined = await clar.incorporate_answers(prd_text, answers)

LLM 호출은 선택적 (V8_PRD_CLARIFY_DEEP=1 시 Haiku 로 추가 스캔).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_PRD_CLARIFY", "1") != "0"
_DEEP = os.environ.get("V8_PRD_CLARIFY_DEEP", "0") != "0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 모호 표현 패턴 — 코드 기반 감지
AMBIGUITY_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (pattern, category, severity)
    (re.compile(r"(약|대략|몇|수십|수백|수천)\s*(개|명|건|장|페이지|화면)"),
     "quantity_vague", "blocking"),
    (re.compile(r"\d+\s*[~～∼\-]\s*\d+\s*(개|명|건|장|페이지|화면|시간|일|주)"),
     "quantity_range", "advisory"),
    (re.compile(r"(필요하면|가능하면|선택적|조건부)\s*(추가|구현|지원)"),
     "priority_conditional", "advisory"),
    (re.compile(r"(주요|핵심|중요)\s*(기능|화면|사용자)"),
     "priority_undefined", "advisory"),
    (re.compile(r"(빠른|느린|충분한|적절한)\s*(성능|속도|응답)"),
     "metric_missing", "advisory"),
    (re.compile(r"관리자(?!.*(유형|권한|역할))"),
     "role_undefined", "advisory"),
    (re.compile(r"(추후|나중에|이후)\s*(결정|정의|확정)"),
     "deferred_decision", "blocking"),
    (re.compile(r"(TBD|TODO|미정)"), "explicit_todo", "blocking"),
    (re.compile(r"(외부|타|제3자)\s*(시스템|서비스|API)(?!.*(예:|예시))"),
     "external_system_unnamed", "advisory"),
]


@dataclass
class Ambiguity:
    category: str
    severity: str   # blocking | advisory
    text: str       # 원문 발견 구간
    context: str    # 주변 텍스트 (앞뒤 80 글자)
    position: int   # PRD 내 위치


@dataclass
class Question:
    id: str
    category: str
    severity: str
    question: str
    options: list[str] = field(default_factory=list)
    context: str = ""


class PRDClarifier:
    def __init__(self, db: Any = None, model_adapter: Any = None) -> None:
        self._db = db
        self._model = model_adapter
        self._enabled = _ENABLED

    async def scan_ambiguities(self, prd_text: str) -> list[Ambiguity]:
        """PRD 에서 모호 표현 패턴 매칭. LLM 호출 0."""
        if not self._enabled or not prd_text:
            return []
        found: list[Ambiguity] = []
        for pat, category, severity in AMBIGUITY_PATTERNS:
            for m in pat.finditer(prd_text):
                start = max(0, m.start() - 80)
                end = min(len(prd_text), m.end() + 80)
                found.append(Ambiguity(
                    category=category,
                    severity=severity,
                    text=m.group(0),
                    context=prd_text[start:end].strip(),
                    position=m.start(),
                ))
        # 중복 제거 (같은 category + 같은 text)
        seen: set[tuple[str, str]] = set()
        uniq: list[Ambiguity] = []
        for a in found:
            key = (a.category, a.text)
            if key not in seen:
                seen.add(key)
                uniq.append(a)
        return uniq

    def to_questions(self, ambiguities: list[Ambiguity]) -> list[Question]:
        """ambiguity → 사용자 질문 생성. 카테고리별 템플릿 사용."""
        qs: list[Question] = []
        for a in ambiguities:
            qid = uuid.uuid4().hex[:12]
            if a.category == "quantity_vague":
                q = Question(
                    id=qid, category=a.category, severity=a.severity,
                    question=f"'{a.text}' 의 정확한 수량을 확정해주세요",
                    options=["정확한 수 입력", "최소~최대 범위"],
                    context=a.context,
                )
            elif a.category == "quantity_range":
                q = Question(
                    id=qid, category=a.category, severity=a.severity,
                    question=f"'{a.text}' 범위를 확정해주세요 (최종 수량 1개)",
                    options=[],
                    context=a.context,
                )
            elif a.category == "role_undefined":
                q = Question(
                    id=qid, category=a.category, severity=a.severity,
                    question="'관리자' 의 유형을 구분하나요?",
                    options=["단일 관리자", "슈퍼/일반 2단계",
                             "권한 매트릭스 (역할별 세분화)"],
                    context=a.context,
                )
            elif a.category == "priority_conditional":
                q = Question(
                    id=qid, category=a.category, severity=a.severity,
                    question=f"'{a.text}' — 이 항목을 1차 범위에 포함할지 결정해주세요",
                    options=["필수 구현 (1차)", "선택 구현 (2차)", "제외"],
                    context=a.context,
                )
            elif a.category == "deferred_decision" or a.category == "explicit_todo":
                q = Question(
                    id=qid, category=a.category, severity="blocking",
                    question=f"'{a.text}' — 이 부분의 결정을 지금 내려주세요",
                    options=[],
                    context=a.context,
                )
            else:
                q = Question(
                    id=qid, category=a.category, severity=a.severity,
                    question=f"'{a.text}' 의 구체 내용을 명시해주세요",
                    options=[],
                    context=a.context,
                )
            qs.append(q)
        return qs

    async def save_questions(
        self, engagement_id: str, questions: list[Question],
    ) -> int:
        """생성한 질문들을 prd_clarifications 테이블에 저장."""
        if not self._db or not questions:
            return 0
        count = 0
        for q in questions:
            try:
                await self._db.execute(
                    """INSERT INTO prd_clarifications
                         (engagement_id, question_id, question, options,
                          severity, category, answer, answered_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                       ON CONFLICT(engagement_id, question_id) DO NOTHING""",
                    (engagement_id, q.id, q.question,
                     json.dumps(q.options, ensure_ascii=False),
                     q.severity, q.category, _now()),
                )
                count += 1
            except Exception as e:
                logger.debug("prd_clar_save_fail %s", e)
        return count

    async def get_unanswered(
        self, engagement_id: str, only_blocking: bool = False,
    ) -> list[dict]:
        """미답변 질문 조회 (UI 에서 노출용)."""
        if not self._db:
            return []
        sql = ("SELECT * FROM prd_clarifications "
               "WHERE engagement_id=? AND answer IS NULL")
        params: list = [engagement_id]
        if only_blocking:
            sql += " AND severity='blocking'"
        sql += " ORDER BY created_at ASC"
        rows = await self._db.fetchall(sql, tuple(params))
        return [dict(r) for r in (rows or [])]

    async def save_answer(
        self, engagement_id: str, question_id: str, answer: str,
    ) -> None:
        if not self._db:
            return
        await self._db.execute(
            """UPDATE prd_clarifications
               SET answer=?, answered_at=?
               WHERE engagement_id=? AND question_id=?""",
            (answer[:2000], _now(), engagement_id, question_id),
        )

    async def incorporate_answers(
        self, prd_text: str, answers: dict[str, str],
    ) -> str:
        """사용자 답변을 PRD 말미에 '정제 사항' 섹션으로 추가.

        LLM merge 는 선택 (V8_PRD_CLARIFY_DEEP=1 시). 기본은 append.
        """
        if not answers:
            return prd_text
        lines = ["\n\n---\n## 정제 사항 (Clarification)\n"]
        for qid, ans in answers.items():
            lines.append(f"- **{qid}**: {ans}")
        return prd_text + "\n".join(lines)

    async def all_blocking_answered(self, engagement_id: str) -> bool:
        """blocking 질문이 모두 답변됐는지 (DESIGN 진입 게이트용)."""
        remaining = await self.get_unanswered(engagement_id, only_blocking=True)
        return len(remaining) == 0
