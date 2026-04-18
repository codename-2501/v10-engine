"""verdict_reconciler 단위 테스트 — S4-2."""
from __future__ import annotations

from engine.skills.qa.verdict_reconciler import (
    MAX_SCORE_RESTORE,
    PASS_THRESHOLD,
    SCORE_BONUS_CRITICAL,
    SCORE_BONUS_HIGH,
    extract_actual_headers,
    reconcile_verdict,
)


# ---------------------------------------------------------------------------
# extract_actual_headers
# ---------------------------------------------------------------------------

RISK_CONTENT_5_SECTIONS = """# 리스크 관리 계획서

## 리스크 식별

리스크 등록부 테이블...

## 리스크 평가

매트릭스...

## 대응 전략

테이블...

## 모니터링 계획

주기·지표...

## 비상 계획

고위험 리스크 비상 절차...
"""


def test_헤더_5개_추출():
    h = extract_actual_headers(RISK_CONTENT_5_SECTIONS)
    assert "리스크 관리 계획서" in h
    assert "리스크 식별" in h
    assert "리스크 평가" in h
    assert "대응 전략" in h
    assert "모니터링 계획" in h
    assert "비상 계획" in h


def test_빈컨텐츠():
    assert extract_actual_headers("") == []
    assert extract_actual_headers(None or "") == []


# ---------------------------------------------------------------------------
# Layer A reconcile — 실제 v4 리스크 케이스 시뮬레이션
# ---------------------------------------------------------------------------

def _build_verdict(score: int, issues: list[dict]) -> dict:
    return {
        "summary": "FAIL" if score < 85 else "PASS",
        "score": score,
        "categories": [
            {"name": "구조", "result": "FAIL", "issues": issues},
        ],
    }


def test_리스크_실전_케이스_PASS_로_복원():
    """AI 가 '대응 전략 누락'이라는데 실제 content 에 ## 대응 전략 있음 → 복원."""
    verdict = _build_verdict(38, [
        {"title": "대응 전략 섹션 전체 누락", "severity": "CRITICAL"},
        {"title": "모니터링 계획 섹션 부재", "severity": "CRITICAL"},
        {"title": "비상 계획 섹션 없음", "severity": "HIGH"},
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    assert info["filtered_count"] == 3
    assert info["score_before"] == 38
    # 38 + 15*2 + 8*1 = 76 (max restore 40 제한 확인)
    # 실제: min(15*2 + 8*1, 40) = min(38, 40) = 38, 38+38=76
    assert info["score_after"] >= 50
    assert new["summary"] == "PASS"


def test_진짜_누락은_그대로_유지():
    """content 에 ## 대응 전략 없으면 AI 판정 그대로."""
    content_short = "# 리스크\n\n## 리스크 식별\n본문\n"
    verdict = _build_verdict(38, [
        {"title": "대응 전략 섹션 전체 누락", "severity": "CRITICAL"},
    ])
    new, info = reconcile_verdict(verdict, content_short)
    assert info["filtered_count"] == 0
    assert new["summary"] == "FAIL"
    assert new["score"] == 38


def test_일부만_false_positive():
    """3건 중 2건은 실제 있음, 1건은 정말 없음 → 2건만 제거, 여전히 FAIL."""
    verdict = _build_verdict(30, [
        {"title": "대응 전략 섹션 누락", "severity": "CRITICAL"},   # 실제 있음
        {"title": "모니터링 계획 섹션 부재", "severity": "HIGH"},   # 실제 있음
        {"title": "용어집 섹션 없음", "severity": "HIGH"},          # 실제 없음
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    assert info["filtered_count"] == 2
    # 30 + 15 + 8 = 53
    assert info["score_after"] >= 50


def test_MAX_RESTORE_상한():
    """많은 false positive 제거해도 최대 +40 까지만 복원."""
    verdict = _build_verdict(10, [
        {"title": f"리스크 {kw} 섹션 누락", "severity": "CRITICAL"}
        for kw in ("식별", "평가", "대응 전략", "모니터링 계획", "비상 계획")
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    # 5 CRITICAL × 15 = 75, but capped at 40
    assert info["score_after"] - info["score_before"] <= MAX_SCORE_RESTORE
    assert info["score_after"] == 10 + MAX_SCORE_RESTORE  # 50


def test_summary_pass_threshold():
    """보정 score가 50 이상이면 PASS 로 변환."""
    verdict = _build_verdict(40, [
        {"title": "리스크 평가 섹션 누락", "severity": "CRITICAL"},
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    # 40 + 15 = 55 ≥ 50
    assert new["summary"] == "PASS"


def test_집계형_claim_처리():
    """'필수 섹션 4개 전체 누락' 같은 집계형 — spec 의 required_headings 와 비교."""
    spec = {
        "validation": {
            "structural": {
                "required_headings": [
                    "리스크 식별", "리스크 평가", "대응 전략",
                    "모니터링 계획", "비상 계획",
                ],
            }
        }
    }
    verdict = _build_verdict(35, [
        {"title": "필수 섹션 4개 전체 누락", "severity": "CRITICAL"},
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS, spec=spec)
    # 5/5 required 실제 존재 → 80% 이상 → false positive
    assert info["filtered_count"] == 1
    assert info["score_after"] >= 50
    assert new["summary"] == "PASS"


def test_원본_보존():
    """raw_original 에 원본 verdict 보존."""
    verdict = _build_verdict(40, [
        {"title": "대응 전략 섹션 누락", "severity": "CRITICAL"},
    ])
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    assert "raw_original" in new
    assert new["raw_original"]["score"] == 40


def test_빈_categories():
    verdict = {"summary": "FAIL", "score": 30, "categories": []}
    new, info = reconcile_verdict(verdict, RISK_CONTENT_5_SECTIONS)
    assert info["filtered_count"] == 0
    assert info["changed"] is False


def test_non_dict_verdict():
    """방어: dict 아닌 input 도 safely 처리."""
    new, info = reconcile_verdict("not a dict", RISK_CONTENT_5_SECTIONS)
    assert new == "not a dict"
    assert info["filtered_count"] == 0
