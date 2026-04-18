"""AI QA verdict ↔ 실제 산출물 cross-check (S4-2 Layer A).

AI QA 가 variance/hallucination 으로 거짓 FAIL 판정하는 케이스 차단.
v4 리스크 관리 계획서 실례: 실제 5 섹션 완비인데 AI 가 "필수 섹션 4개 전체
누락" 판정 → SUSPENDED. 이 모듈이 실제 content 헤더를 직접 감지 후 AI 주장과
교차검증 → false positive 제거 → score 복원.

설계:
- AI 무조건 신뢰 금지 — LLM 은 긴 문서 읽기에서 hallucinate
- 실제 content 의 `##` 헤더는 결정적 (regex) → ground truth
- issue.title/description 에서 "X 섹션 누락" 패턴 추출 → 헤더와 매칭
- 일치하면 false positive 로 마킹 + CRITICAL/HIGH 였다면 점수 복원

복원 공식:
- false_critical_removed × +15점
- false_high_removed × +8점
- 최대 +40점 까지 (남은 true issue 가 있을 수 있어 과복원 방지)

원본 verdict 는 `raw_original` 필드에 보존 — 감사·디버그 가능.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# harness.py 와 동일 regex — ground truth 일관성
_HEADER_RE = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.MULTILINE)

# issue.title 에서 "X 섹션 (누락|...)" 패턴 추출
_SECTION_CLAIM_RE = re.compile(
    r"([가-힣A-Za-z][가-힣A-Za-z0-9 \-/]{1,40}?)\s*"
    r"섹션\s*(?:전체\s*)?"
    r"(누락|미완성|부재|없음|missing|전무|누락됨)",
    re.IGNORECASE,
)

# 부정 키워드 (issue 본문에서 누락 주장 감지)
_MISSING_KEYWORDS = (
    "누락", "부재", "없음", "missing", "전무", "작성되지 않", "포함되지 않",
)

# 점수 복원 상한 (과복원 방지)
SCORE_BONUS_CRITICAL = 15
SCORE_BONUS_HIGH = 8
MAX_SCORE_RESTORE = 40
PASS_THRESHOLD = 50


def extract_actual_headers(content: str) -> list[str]:
    """content 에서 `##` 시작 헤더 텍스트만 list 로. 정규화 (trim)."""
    if not content:
        return []
    out: list[str] = []
    for m in _HEADER_RE.finditer(content):
        t = (m.group(1) or "").strip()
        if t and len(t) < 100:
            out.append(t)
    return out


def _is_false_positive(issue: dict, actual_headers: list[str]) -> tuple[bool, str]:
    """이 issue 가 '섹션 X 누락' 주장인데 실제 X 헤더 있으면 → false positive.

    Returns: (is_false, matched_header).
    """
    if not actual_headers:
        return False, ""
    title = str(issue.get("title") or "")
    desc = str(issue.get("description") or "")
    actual = str(issue.get("actual") or "")
    combined = f"{title} {desc} {actual}"

    # "X 섹션 누락" 패턴에서 X 추출
    for m in _SECTION_CLAIM_RE.finditer(combined):
        claimed_name = (m.group(1) or "").strip()
        if not claimed_name:
            continue
        # 실제 헤더와 매치 (부분 or 완전)
        cn_low = claimed_name.lower()
        for h in actual_headers:
            h_low = h.lower()
            # 양방향 부분 매칭 (한국어 조사 접촉 허용)
            if cn_low in h_low or h_low in cn_low:
                return True, h

    # "필수 섹션 N개 누락" 같은 집계형 주장 — 실제 헤더 수가 충분하면 false
    # 단, spec.structural.required_headings 를 caller 가 넘겨주지 않으면 판단 불가
    # → 집계형은 보수적으로 처리 (false 로 단정 안 함)
    return False, ""


def reconcile_verdict(
    verdict: dict,
    actual_content: str,
    spec: dict | None = None,
) -> tuple[dict, dict]:
    """AI verdict 와 실제 content cross-check → 보정된 verdict.

    Args:
        verdict: _parse_qa_verdict 결과. `{summary, score, categories: [{issues: [...]}]}`
        actual_content: QA 대상 산출물 원본 (문자열)
        spec: spec dict — required_headings 기반 집계형 claim 판정용 (선택)

    Returns:
        (new_verdict, info)
        - new_verdict: false positive 제거 + score 보정 후
        - info: {filtered_count, removed_issues, score_before, score_after, headers_found}
    """
    if not isinstance(verdict, dict):
        return verdict, {
            "filtered_count": 0, "removed_issues": [],
            "score_before": 0, "score_after": 0,
            "headers_found": 0, "changed": False,
        }
    info: dict[str, Any] = {
        "filtered_count": 0,
        "removed_issues": [],
        "score_before": verdict.get("score", 0),
        "score_after": verdict.get("score", 0),
        "headers_found": 0,
        "changed": False,
    }

    actual_headers = extract_actual_headers(actual_content or "")
    info["headers_found"] = len(actual_headers)

    if not actual_headers:
        # 헤더 전무 → reconcile 불가 (AI 판정 그대로)
        return verdict, info

    categories = verdict.get("categories") or []
    if not isinstance(categories, list):
        return verdict, info

    # 집계형 claim ("필수 섹션 N개 누락") 보정 로직
    required_headings = []
    if spec and isinstance(spec, dict):
        structural = spec.get("validation", {}).get("structural", {})
        required_headings = structural.get("required_headings") or []
    required_actually_present = 0
    if required_headings:
        for rh in required_headings:
            rh_low = rh.lower()
            if any(rh_low in h.lower() or h.lower() in rh_low for h in actual_headers):
                required_actually_present += 1

    false_critical = 0
    false_high = 0
    removed_issues: list[dict] = []
    new_categories: list[dict] = []

    for cat in categories:
        if not isinstance(cat, dict):
            new_categories.append(cat)
            continue
        issues = cat.get("issues") or []
        kept_issues: list[dict] = []
        for iss in issues:
            if not isinstance(iss, dict):
                kept_issues.append(iss)
                continue
            is_false, matched = _is_false_positive(iss, actual_headers)
            # 집계형 ("필수 섹션 N개 누락") — required_headings 대비 거의 다 있으면 false
            if not is_false and required_headings:
                title = str(iss.get("title") or "")
                if re.search(r"(필수\s*섹션|required\s*sections?).*(\d+\s*개|전체).*누락",
                             title, re.IGNORECASE):
                    if required_actually_present >= len(required_headings) * 0.8:
                        is_false = True
                        matched = f"aggregate({required_actually_present}/{len(required_headings)})"

            if is_false:
                sev = (iss.get("severity") or "").upper()
                if sev == "CRITICAL":
                    false_critical += 1
                elif sev == "HIGH":
                    false_high += 1
                removed_issues.append({
                    "title": iss.get("title"),
                    "severity": sev,
                    "matched_header": matched,
                })
                continue
            kept_issues.append(iss)
        new_cat = dict(cat)
        new_cat["issues"] = kept_issues
        # 카테고리 issues 전부 제거되면 result 도 PASS 고려
        if not kept_issues and cat.get("result") == "FAIL":
            new_cat["result"] = "PASS"
        new_categories.append(new_cat)

    info["filtered_count"] = len(removed_issues)
    info["removed_issues"] = removed_issues

    if not removed_issues:
        return verdict, info

    # score 복원
    bonus = false_critical * SCORE_BONUS_CRITICAL + false_high * SCORE_BONUS_HIGH
    bonus = min(bonus, MAX_SCORE_RESTORE)
    new_score = min(100, int(verdict.get("score", 0) or 0) + bonus)

    new_verdict = dict(verdict)
    new_verdict["categories"] = new_categories
    new_verdict["score"] = new_score
    new_verdict["raw_original"] = {
        "score": verdict.get("score"),
        "summary": verdict.get("summary"),
        "categories": categories,
    }
    # summary 재판정
    if new_score >= PASS_THRESHOLD:
        new_verdict["summary"] = "PASS"
    else:
        new_verdict["summary"] = verdict.get("summary", "FAIL")

    info["score_after"] = new_score
    info["changed"] = True

    logger.info(
        "ai_false_fail_filtered count=%d crit=%d high=%d score_before=%d score_after=%d matched_headers=%s",
        len(removed_issues), false_critical, false_high,
        info["score_before"], new_score,
        [r["matched_header"] for r in removed_issues[:5]],
    )
    return new_verdict, info
