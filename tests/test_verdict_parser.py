"""verdict_parser 단위 테스트 — S4-1.

실제 v3 에서 발생한 verdict text 를 그대로 입력해 회귀 차단.
"""
from __future__ import annotations

from engine.skills.qa.verdict_parser import (
    extract_failure_summary,
    extract_from_categories,
    extract_missing_sections,
)


# ---------------------------------------------------------------------------
# 실전 케이스 — v3 리스크 노드 (2026-04-15)
# ---------------------------------------------------------------------------

RISK_VERDICT_1 = (
    "QA 부분 패치 (score=42): 리스크 평가 섹션 미완성 — 문장 중간 절단; "
    "대응 전략 섹션 전체 누락; 모니터링 계획 섹션 전체 누락"
)

RISK_SECTIONS = [
    "리스크 식별", "리스크 평가", "대응 전략",
    "모니터링 계획", "비상 계획",
]


def test_리스크_케이스_known_names_매칭():
    out = extract_missing_sections(RISK_VERDICT_1, RISK_SECTIONS)
    assert "리스크 평가" in out
    assert "대응 전략" in out
    assert "모니터링 계획" in out
    # 통과 섹션은 잡히지 않아야
    assert "리스크 식별" not in out
    assert "비상 계획" not in out


def test_리스크_케이스_known_없이도_정규식_매칭():
    out = extract_missing_sections(RISK_VERDICT_1, None)
    # known 없어도 "X 섹션 누락" 패턴은 잡혀야 함
    assert any("대응 전략" in s for s in out)
    assert any("모니터링 계획" in s for s in out)


# ---------------------------------------------------------------------------
# v3 요구사항 정의서 케이스 (2026-04-15)
# ---------------------------------------------------------------------------

REQ_VERDICT_1 = (
    "QA 부분 패치 (score=38): P0/P1/P2 우선순위 전면 누락; "
    "우선순위 판정 기준 섹션 부재; 스토리포인트 전면 누락"
)

REQ_SECTIONS = [
    "기능 백로그", "우선순위 판정 기준", "스토리포인트 산정",
    "비기능 요구사항", "수용 기준",
]


def test_요구사항_known_names_매칭():
    out = extract_missing_sections(REQ_VERDICT_1, REQ_SECTIONS)
    assert "우선순위 판정 기준" in out
    # P0/P1/P2 는 섹션 명이 아니라 항목이라 정확히 잡힐 필요는 없음
    # 핵심은 "X 섹션 부재" 패턴이 잡히는 것


# ---------------------------------------------------------------------------
# 부정 키워드 종류
# ---------------------------------------------------------------------------

def test_미완성():
    out = extract_missing_sections("결과 섹션 미완성", ["결과"])
    assert "결과" in out


def test_없음():
    out = extract_missing_sections("결론 섹션 없음", ["결론"])
    assert "결론" in out


def test_missing_영문():
    out = extract_missing_sections("Conclusion 섹션 missing", ["Conclusion"])
    assert "Conclusion" in out


def test_부정_키워드_없으면_X():
    """단순 언급은 잡히지 않아야."""
    out = extract_missing_sections("결론 섹션 작성 완료", ["결론"])
    assert out == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_빈입력():
    assert extract_missing_sections("") == []
    assert extract_missing_sections(None or "") == []


def test_known_없는_섹션_무시():
    """known list 에 없는 섹션은 정규식 fallback 에서만 잡힘."""
    out = extract_missing_sections(
        "임의 섹션 누락", known_section_names=["완전히_다른"],
    )
    # "임의" 는 known 에 없지만 정규식으로 잡힘
    assert any("임의" in s for s in out)


def test_중복_제거():
    """같은 섹션 두 번 언급 → 1번만."""
    out = extract_missing_sections(
        "대응 전략 섹션 누락; 또 대응 전략 섹션 미완성",
        ["대응 전략"],
    )
    assert out.count("대응 전략") == 1


# ---------------------------------------------------------------------------
# extract_failure_summary
# ---------------------------------------------------------------------------

def test_summary_점수_누락섹션():
    s = extract_failure_summary(RISK_VERDICT_1)
    assert s["score"] == 42
    assert "대응 전략" in s["missing_sections"] or any(
        "대응 전략" in m for m in s["missing_sections"]
    )
    assert "summary" in s and len(s["summary"]) > 0


def test_summary_빈입력():
    s = extract_failure_summary("")
    assert s == {"score": None, "missing_sections": [], "summary": ""}


# ---------------------------------------------------------------------------
# 구조화 입력 (categories.issues)
# ---------------------------------------------------------------------------

def test_categories_입력():
    cats = [
        {
            "name": "구조",
            "result": "FAIL",
            "issues": [
                {"title": "대응 전략 섹션 전체 누락", "severity": "CRITICAL"},
                {"title": "모니터링 계획 섹션 부재", "severity": "HIGH"},
            ],
        }
    ]
    out = extract_from_categories(cats, RISK_SECTIONS)
    assert "대응 전략" in out
    assert "모니터링 계획" in out


def test_categories_빈입력():
    assert extract_from_categories([]) == []
    assert extract_from_categories(None or []) == []


# ---------------------------------------------------------------------------
# 조사 처리
# ---------------------------------------------------------------------------

def test_한국어_조사_접촉():
    """'대응 전략은' / '대응 전략을' 같은 조사 붙어도 매칭."""
    out = extract_missing_sections(
        "대응 전략은 섹션 누락", ["대응 전략"],
    )
    assert "대응 전략" in out
