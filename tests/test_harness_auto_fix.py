"""Harness 자동수정 (forbidden_words) 단위 테스트 — S1-5.

핵심 회귀 방지 포인트:
- 한국어 조사 매칭 (TBD로 → 미지정으로)
- safe_regions(코드블록/heading/URL) 보호
- MAX 초과 시 no-op
- 감사 마커 주석 삽입

실행: pytest tests/test_harness_auto_fix.py -v
"""
from __future__ import annotations

from engine.skills.qa.harness_auto_fix import (
    MAX_AUTO_FIX_FORBIDDEN,
    try_auto_fix_forbidden_words,
)


# ---------------------------------------------------------------------------
# 기본 치환
# ---------------------------------------------------------------------------

def test_tbd_단독_치환():
    r = try_auto_fix_forbidden_words("이 항목은 TBD 입니다.")
    assert r.applied is True
    assert "미지정" in r.new_content
    assert "TBD" not in r.new_content.split("<!--")[0]


def test_tbd_조사_매칭():
    """TBD로 → 미지정으로 (조사 우선 규칙)."""
    r = try_auto_fix_forbidden_words("값은 TBD로 처리한다.")
    assert r.applied is True
    assert "미지정으로" in r.new_content


def test_todo_치환():
    r = try_auto_fix_forbidden_words("TODO 다음 단계 진행")
    assert r.applied is True
    # TODO만 매치 (TODO: 패턴 아님)
    assert "다음 단계 진행 예정" in r.new_content


def test_한국어_placeholder():
    r = try_auto_fix_forbidden_words("이 부분은 추후 작성 합니다.")
    assert r.applied is True
    assert "다음 단계에서 진행" in r.new_content


# ---------------------------------------------------------------------------
# Safe regions — 코드블록/heading/URL 보호
# ---------------------------------------------------------------------------

def test_코드블록_내_TBD_보존():
    src = "본문 TBD\n```\nlet x = TBD;\n```\n"
    r = try_auto_fix_forbidden_words(src)
    # 본문 TBD 는 치환, 코드블록 안 TBD 는 보존
    assert r.applied is True
    assert "let x = TBD;" in r.new_content
    assert "본문 미지정" in r.new_content


def test_heading_내_TBD_보존():
    src = "## TBD 섹션\n본문 TBD"
    r = try_auto_fix_forbidden_words(src)
    assert "## TBD 섹션" in r.new_content
    assert "본문 미지정" in r.new_content


def test_URL_내_TBD_보존():
    src = "참조 https://example.com/TBD/path 와 본문 TBD"
    r = try_auto_fix_forbidden_words(src)
    assert "https://example.com/TBD/path" in r.new_content
    assert "본문 미지정" in r.new_content


# ---------------------------------------------------------------------------
# 남용 가드
# ---------------------------------------------------------------------------

def test_남용_시_원본_반환():
    """치환 횟수 > MAX 이면 self-disable (LLM 출력 자체가 문제)."""
    over = "TBD " * (MAX_AUTO_FIX_FORBIDDEN + 2)
    r = try_auto_fix_forbidden_words(over)
    assert r.applied is False
    assert r.skipped_reason == "exceeds_max"
    assert r.new_content == over  # 원본 그대로


def test_매치_없으면_no_op():
    r = try_auto_fix_forbidden_words("정상 한국어 본문입니다.")
    assert r.applied is False
    assert r.skipped_reason == "no_matches"
    assert r.count == 0


def test_빈_문자열():
    r = try_auto_fix_forbidden_words("")
    assert r.applied is False
    assert r.skipped_reason == "empty_content"


# ---------------------------------------------------------------------------
# 감사 마커
# ---------------------------------------------------------------------------

def test_감사_마커_삽입():
    r = try_auto_fix_forbidden_words("값은 TBD 임")
    assert "harness_auto_fix" in r.new_content
    assert "forbidden_words" in r.new_content


# ---------------------------------------------------------------------------
# Word-boundary 회귀 — ASCII identifier 손상 금지
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# S2-6 확장 — lorem / missing_headings / short_table / pipeline
# ---------------------------------------------------------------------------

def test_lorem_치환():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_lorem_ipsum
    r = try_auto_fix_lorem_ipsum(
        "본문 시작. Lorem ipsum dolor sit amet. 끝.",
        project_name="실버케어",
    )
    assert r.applied is True
    assert "Lorem ipsum" not in r.new_content.split("<!--")[0]
    assert "실버케어" in r.new_content


def test_lorem_매치_없으면_no_op():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_lorem_ipsum
    r = try_auto_fix_lorem_ipsum("정상 본문 100% 한국어")
    assert r.applied is False


def test_missing_headings_삽입():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_missing_headings
    src = "## 개요\n\n본문\n\n## 결론\n"
    r = try_auto_fix_missing_headings(src, ["개요", "상세", "결론", "부록"])
    assert r.applied is True
    assert r.count == 2
    assert "## 상세" in r.new_content
    assert "## 부록" in r.new_content


def test_missing_headings_전부_있으면_no_op():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_missing_headings
    r = try_auto_fix_missing_headings("## A\n## B", ["A", "B"])
    assert r.applied is False


def test_missing_headings_많으면_포기():
    """5개 초과 누락이면 LLM 재생성이 더 합리적 → 포기."""
    from engine.skills.qa.harness_auto_fix import try_auto_fix_missing_headings
    r = try_auto_fix_missing_headings(
        "본문\n", ["A", "B", "C", "D", "E", "F", "G"],
    )
    assert r.applied is False
    assert r.skipped_reason == "too_many_missing"


def test_short_table_자동행_채움():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_short_table
    src = (
        "본문\n\n"
        "| ID | 이름 | 설명 |\n"
        "|---|---|---|\n"
        "| SC-1 | A | 설명1 |\n"
        "\n끝"
    )
    r = try_auto_fix_short_table(
        src, min_rows=3, outline_ids=["SC-2", "SC-3", "SC-4"],
    )
    assert r.applied is True
    assert r.count == 2  # 1행 → 3행 부족분 2
    assert "SC-2" in r.new_content
    assert "SC-3" in r.new_content


def test_short_table_outline_없으면_no_op():
    from engine.skills.qa.harness_auto_fix import try_auto_fix_short_table
    r = try_auto_fix_short_table("| a |\n|---|", min_rows=10, outline_ids=[])
    assert r.applied is False


def test_pipeline_여러_fix_누적():
    from engine.skills.qa.harness_auto_fix import run_auto_fix_pipeline
    src = "TBD 항목. Lorem ipsum dolor sit amet. ## A 만 있음."
    r = run_auto_fix_pipeline(
        src, required_headings=["A", "Z"], project_name="X",
    )
    # forbidden(1) + lorem(1) + missing_heading 'Z'(1) 누적
    assert r.applied is True
    assert r.count >= 3


def test_식별자_안의_TBD_보존():
    """myTBDvar / TBD_FLAG 같은 식별자는 치환 X."""
    src = "let myTBDvar = 1; const TBD_FLAG = true;"
    r = try_auto_fix_forbidden_words(src)
    # 매치 0 → 적용 안 됨
    assert r.applied is False
    assert "myTBDvar" in r.new_content
    assert "TBD_FLAG" in r.new_content
