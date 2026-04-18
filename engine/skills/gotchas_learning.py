"""Failure-pattern learning (S3-8).

이전 engagement 에서 반복 발생한 QA gotcha 를 카테고리화 → 향후 동일 spec
prompt 에 "과거 이 실수 빈발" 경고 자동 주입. 같은 실수 반복 차단.

데이터 소스:
- project_gotchas (또는 audit_log 의 retry/cascade 항목)에서 카테고리·키워드
  추출
- 카테고리 = (spec_name, failure_keyword) 튜플
- 임계 N회 이상 발생한 카테고리만 prompt 에 주입 (노이즈 차단)

설계:
- 코어 무수정 — executor 가 prompt 조립 시 import 해서 사용
- LLM 호출 X (순수 SQL 집계 + 텍스트 매칭)
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)


# 카테고리별 키워드 (정규식). 키워드 1회 이상 매치 시 해당 카테고리 카운트.
GOTCHA_CATEGORIES: dict[str, re.Pattern] = {
    "id_group_confusion": re.compile(
        r"(SC-[A-Z]{2,4}-\d+|SCR-\d+).{0,30}(혼동|혼란|일관|불일치|inconsistent)",
        re.IGNORECASE,
    ),
    "missing_section": re.compile(
        r"(missing|누락|결손).{0,20}(section|섹션)", re.IGNORECASE,
    ),
    "page_count_mismatch": re.compile(
        r"(\d{2,3}).{0,10}(개|장).{0,30}(불일치|≠|\!=|mismatch)", re.IGNORECASE,
    ),
    "forbidden_word_repeat": re.compile(
        r"(TBD|TODO|FIXME).{0,30}(반복|재출현|repeat)", re.IGNORECASE,
    ),
    "min_items_short": re.compile(
        r"(min_items|최소.{0,5}항목).{0,30}(부족|미달|short)", re.IGNORECASE,
    ),
    "table_row_short": re.compile(
        r"(테이블|table).{0,30}(행|row).{0,20}(부족|미달)", re.IGNORECASE,
    ),
    "design_match_fail": re.compile(
        r"(design|디자인).{0,20}(불일치|mismatch|miss)", re.IGNORECASE,
    ),
    "screen_coverage_low": re.compile(
        r"(coverage|커버리지).{0,30}(미달|낮|low|insufficient)", re.IGNORECASE,
    ),
}


# 카테고리별 prompt 경고문 (LLM 에게 보낼 1줄 hint).
GOTCHA_HINTS: dict[str, str] = {
    "id_group_confusion": (
        "과거 동일 spec 에서 SC-XX 그룹 코드(SC-AU/CW/WA 등)를 혼동해 ID "
        "체계가 깨진 사례 빈발. 그룹 접두를 일관되게 유지할 것."
    ),
    "missing_section": (
        "과거 필수 섹션 1개 이상이 누락된 사례 빈발. spec 에 명시된 모든 "
        "섹션을 빠짐없이 작성할 것."
    ),
    "page_count_mismatch": (
        "과거 화면 수가 섹션 간 불일치(예: 47 vs 50)했던 사례 빈발. "
        "outline 에 명시된 항목 수와 정확히 일치시킬 것."
    ),
    "forbidden_word_repeat": (
        "과거 TBD/TODO/FIXME 등 placeholder 를 반복 삽입한 사례 빈발. "
        "결정되지 않은 사항은 '미지정' 또는 '다음 단계에서 진행'으로 표현."
    ),
    "min_items_short": (
        "과거 spec 의 min_items 미달이 빈발. 항목 수를 spec 기준 이상으로 "
        "충실히 채울 것."
    ),
    "table_row_short": (
        "과거 표 데이터 행이 부족했던 사례 빈발. 표 헤더 외 데이터 행을 "
        "충분히 작성할 것."
    ),
    "design_match_fail": (
        "과거 디자인 시안과 코드 산출물이 불일치한 사례 빈발. 디자인 컴포넌트 "
        "이름·구조를 정확히 반영할 것."
    ),
    "screen_coverage_low": (
        "과거 화면 커버리지가 낮았던 사례 빈발. 모든 화면 ID 를 산출물에 "
        "참조할 것."
    ),
}


async def aggregate_gotchas(
    db: Any,
    spec_name: str,
    lookback_days: int = 30,
) -> dict[str, int]:
    """spec_name 에 대해 최근 N일 누적된 카테고리별 발생 횟수.

    project_gotchas 또는 nodes.description (FAIL verdict) 에서 텍스트 추출
    후 카테고리 정규식 매칭.
    """
    if db is None:
        return {}
    counts: Counter[str] = Counter()
    try:
        rows = await db.fetchall(
            """SELECT description FROM nodes
            WHERE state IN ('INVALID','FAILED','SUSPENDED')
              AND task_name LIKE ?
              AND updated_at > datetime('now', ?)
              AND description IS NOT NULL
            LIMIT 200""",
            (f"%{spec_name}%", f"-{lookback_days} days"),
        )
        for r in rows:
            text = (r.get("description") or "")[:2000]
            for cat, pat in GOTCHA_CATEGORIES.items():
                if pat.search(text):
                    counts[cat] += 1
    except Exception as e:
        logger.warning("aggregate_gotchas failed: %s", e)
    return dict(counts)


def build_gotcha_hints(counts: dict[str, int], min_occurrences: int = 2) -> str:
    """집계된 카테고리에서 임계 이상만 prompt hint 로 조립.

    Returns: prompt 에 append 가능한 마크다운 블록 (또는 '').
    """
    relevant = [
        (cat, n) for cat, n in counts.items()
        if n >= min_occurrences and cat in GOTCHA_HINTS
    ]
    if not relevant:
        return ""
    relevant.sort(key=lambda x: -x[1])
    lines = ["", "## ⚠ 과거 실패 패턴 (반복 방지)", ""]
    for cat, n in relevant[:5]:  # 상위 5개만
        lines.append(f"- **[{n}회 발생]** {GOTCHA_HINTS[cat]}")
    lines.append("")
    return "\n".join(lines)


async def get_hints_for_spec(
    db: Any, spec_name: str, lookback_days: int = 30,
) -> str:
    """원샷 헬퍼 — executor 에서 prompt 조립 직전 호출."""
    counts = await aggregate_gotchas(db, spec_name, lookback_days)
    return build_gotcha_hints(counts)
