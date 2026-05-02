"""QA verdict → 누락 섹션 명 추출 (S4-1).

QA 가 발견한 "어떤 섹션이 누락/미완성됐는가" 를 정확히 추출해 다음 retry 시
chunked_document 섹션별 호출 prompt 최상단에 강조 주입하기 위함.

기존 affected_sections 컬럼은 검증 항목명("required_sections", "no_todo")이라
실제 마크다운 섹션 명("대응 전략", "모니터링 계획")으로 매핑 불가 → AI 가
누구를 고치라는지 모름. 이 모듈이 verdict text 에서 진짜 섹션 명을 뽑아 그
빈자리를 메움.

3중 매칭:
1. known_section_names 가 있으면 그 이름이 verdict_text 에 등장 + 부정 키워드
   인접 → 강한 신호 (false positive 최소)
2. 정규식 매칭 — "X 섹션 (전체) 누락/미완성/없음/missing" 패턴
3. 구조화 입력 (categories.issues.title) 도 동일 흐름

설계 원칙:
- 코어 무수정 — engine/skills/ 안에서만
- 빈 입력/오류는 빈 list 반환 (caller 무영향)
- 한국어 조사 접촉 안전 (Korean-aware boundary)
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# 정규식
# ────────────────────────────────────────────────────────────────────────

# 부정 키워드 (섹션이 없거나 부족함을 나타냄)
_NEGATIVE_KEYWORDS = (
    "전체 누락", "전체누락", "통째로 누락",
    "누락", "미완성", "부재", "없음", "missing",
    "전면 누락", "전면누락", "completely missing",
    "절단", "truncated", "incomplete",
)

# 섹션 이름 후보 — "X 섹션 ..." 패턴
# Korean 2~20자 또는 ASCII 단어/공백 — "섹션" 직전까지
_SECTION_PATTERN = re.compile(
    r"([가-힣A-Za-z][가-힣A-Za-z0-9 \-/]{1,30}?)\s*섹션",
)

# 부정 키워드 인접 검사 거리 (섹션 이름 뒤 N글자 이내)
_NEG_PROXIMITY = 30


# ────────────────────────────────────────────────────────────────────────
# 핵심 함수
# ────────────────────────────────────────────────────────────────────────

def extract_missing_sections(
    verdict_text: str,
    known_section_names: list[str] | None = None,
) -> list[str]:
    """verdict text 에서 누락/미완성 섹션 명 list 추출.

    Args:
        verdict_text: QA fail 사유 (자유 텍스트). e.g.
            "리스크 평가 섹션 미완성; 대응 전략 섹션 전체 누락; 모니터링 계획 섹션 전체 누락"
        known_section_names: spec.sections 의 이름 list. 있으면 우선 매칭.

    Returns:
        누락된 섹션 명 list (중복 제거, 등장 순서 유지). 빈 입력 → [].
    """
    if not verdict_text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    # 1) known names 우선 매칭 (false positive 최소)
    if known_section_names:
        for name in known_section_names:
            if not name:
                continue
            if _name_with_negative_nearby(verdict_text, name):
                if name not in seen:
                    found.append(name)
                    seen.add(name)

    # 2) 정규식 매칭 — "X 섹션 ... 누락/미완성"
    for m in _SECTION_PATTERN.finditer(verdict_text):
        candidate = m.group(1).strip()
        if not candidate or len(candidate) < 2:
            continue
        # 너무 일반적인 단어는 제외
        if candidate in {"각", "그", "이", "저", "필수", "해당", "전체"}:
            continue
        # 섹션 이름 뒤 NEG_PROXIMITY 자 이내에 부정 키워드?
        end = m.end()
        tail = verdict_text[end:end + _NEG_PROXIMITY]
        if not _has_negative_keyword(tail):
            continue
        # 정규화 (좌우 trim, 한국어 조사 제거)
        candidate = _strip_particles(candidate)
        if not candidate or candidate in seen:
            continue
        # known_names 와 부분 매치되면 known 쪽 우선
        if known_section_names:
            matched = _match_known(candidate, known_section_names)
            if matched and matched not in seen:
                found.append(matched)
                seen.add(matched)
                continue
            elif matched:
                continue
        found.append(candidate)
        seen.add(candidate)

    return found


def extract_failure_summary(verdict_text: str) -> dict:
    """verdict 에서 점수·짧은 사유·누락 섹션 한 번에 추출."""
    if not verdict_text:
        return {"score": None, "missing_sections": [], "summary": ""}

    score = None
    m_score = re.search(r"score[=:\s]*(\d{1,3})", verdict_text, re.IGNORECASE)
    if m_score:
        try:
            score = max(0, min(100, int(m_score.group(1))))
        except ValueError:
            pass

    missing = extract_missing_sections(verdict_text)
    summary = verdict_text[:200].replace("\n", " ").strip()
    return {"score": score, "missing_sections": missing, "summary": summary}


def extract_from_categories(
    categories: list[dict],
    known_section_names: list[str] | None = None,
) -> list[str]:
    """구조화 verdict (categories.issues.title) 에서 누락 섹션 추출.

    AI QA 응답이 dict 형태일 때 사용. issues 의 title/description 을
    자유 텍스트로 합쳐 extract_missing_sections 호출.
    """
    if not categories:
        return []
    chunks: list[str] = []
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        for iss in cat.get("issues") or []:
            if not isinstance(iss, dict):
                continue
            for k in ("title", "description", "actual"):
                v = iss.get(k)
                if isinstance(v, str) and v:
                    chunks.append(v)
    if not chunks:
        return []
    combined = "; ".join(chunks)
    return extract_missing_sections(combined, known_section_names)


# 기본 item_key fallback regex — SC-/SCR-/FR-/API-/UC- 등 흔한 포맷 포괄.
# spec 의 chunk_items_source.extract_patterns 가 있으면 그쪽 우선.
_DEFAULT_ITEM_KEY_PATTERNS = [
    r"(?<![A-Za-z0-9_])(SC-[A-Z]{2,5}-\d{3,4})(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])(SCR-[A-Z]{0,5}-?\d{3,4})(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])([A-Z]{2,5}-[A-Z]{2,5}-\d{3,4})(?![A-Za-z0-9_])",
]


def extract_failed_item_keys(
    verdict_input: str | list | dict,
    patterns: list[str] | None = None,
) -> list[str]:
    """QA verdict 에서 chunked items 의 item_key 목록 추출.

    chunked html/json items 스킬의 partial patch 용. spec 의
    `chunk_items_source.extract_patterns` 를 동적으로 받아 정규식 적용.
    전달된 patterns 가 없으면 기본 fallback 셋 사용.

    Args:
        verdict_input:
            - str: QA fail 사유 text. 예: "SC-HB-005 섹션 깨짐; SC-HB-012 빈 껍데기"
            - list[dict]: QA verdict 의 categories 구조 (issues.title/description/actual 합침)
            - dict: categories 키를 담은 최상위 verdict dict
        patterns: spec.chunk_items_source.extract_patterns 리스트. None 이면 기본 셋.

    Returns:
        등장 순서 중복 제거된 item_key 리스트. 빈 입력 또는 매치 없음 → [].
    """
    if not verdict_input:
        return []

    # 1. 입력을 단일 text 로 정규화
    if isinstance(verdict_input, str):
        combined = verdict_input
    elif isinstance(verdict_input, dict):
        cats = verdict_input.get("categories") or []
        combined = _join_category_text(cats)
    elif isinstance(verdict_input, list):
        combined = _join_category_text(verdict_input)
    else:
        return []

    if not combined:
        return []

    # 2. 패턴 준비 — spec 의 extract_patterns 있으면 그것만, 없으면 기본 fallback
    regexes: list[re.Pattern] = []
    for pat in (patterns or []):
        if isinstance(pat, str) and pat:
            try:
                regexes.append(re.compile(pat))
            except re.error:
                continue
    if not regexes:
        regexes = [re.compile(p) for p in _DEFAULT_ITEM_KEY_PATTERNS]

    # 3. 각 regex 로 매칭 + 등장순 중복 제거
    found: list[str] = []
    seen: set[str] = set()
    for rx in regexes:
        for m in rx.finditer(combined):
            key = m.group(1) if m.groups() else m.group(0)
            if key and key not in seen:
                found.append(key)
                seen.add(key)

    return found


def _join_category_text(categories: list) -> str:
    """categories[].issues 의 title/description/actual 필드를 합친 text."""
    chunks: list[str] = []
    for cat in categories or []:
        if not isinstance(cat, dict):
            continue
        for iss in cat.get("issues") or []:
            if not isinstance(iss, dict):
                continue
            for k in ("title", "description", "actual"):
                v = iss.get(k)
                if isinstance(v, str) and v:
                    chunks.append(v)
    return "; ".join(chunks)


# ────────────────────────────────────────────────────────────────────────
# 헬퍼
# ────────────────────────────────────────────────────────────────────────

def _has_negative_keyword(text: str) -> bool:
    text_l = (text or "").lower()
    return any(kw.lower() in text_l for kw in _NEGATIVE_KEYWORDS)


def _name_with_negative_nearby(verdict: str, name: str) -> bool:
    """verdict 에서 name 등장 + 뒤 NEG_PROXIMITY 자 이내에 부정 키워드."""
    name_low = name.lower()
    verdict_low = verdict.lower()
    idx = 0
    while True:
        pos = verdict_low.find(name_low, idx)
        if pos < 0:
            return False
        # name 뒤 검사 영역
        tail = verdict_low[pos + len(name_low):pos + len(name_low) + _NEG_PROXIMITY]
        if _has_negative_keyword(tail):
            return True
        idx = pos + len(name_low)


def _strip_particles(name: str) -> str:
    """한국어 조사 trim — '대응 전략은' → '대응 전략'."""
    name = (name or "").strip()
    for particle in ("은", "는", "이", "가", "을", "를", "의", "에", "와", "과"):
        if name.endswith(particle) and len(name) > len(particle) + 1:
            return name[:-len(particle)].rstrip()
    return name


def _match_known(candidate: str, known: list[str]) -> str | None:
    """candidate 가 known 중 어느 것의 부분 매치인지 — 부분 매치되면 그 known 반환."""
    cand_low = candidate.lower()
    for k in known:
        if not k:
            continue
        kl = k.lower()
        # 양방향 부분 매치
        if cand_low in kl or kl in cand_low:
            return k
    return None
