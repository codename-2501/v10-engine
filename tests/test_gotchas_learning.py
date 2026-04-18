"""gotchas_learning 단위 테스트 — S3-8."""
from __future__ import annotations

from engine.skills.gotchas_learning import (
    GOTCHA_CATEGORIES,
    GOTCHA_HINTS,
    build_gotcha_hints,
)


def test_카테고리_훈트_매칭():
    """모든 카테고리는 hint 가 정의되어 있어야 함."""
    for cat in GOTCHA_CATEGORIES:
        assert cat in GOTCHA_HINTS


def test_id_그룹_혼동_매치():
    pat = GOTCHA_CATEGORIES["id_group_confusion"]
    assert pat.search("SC-AU-001과 SC-CW-001 혼동")


def test_missing_section_매치():
    pat = GOTCHA_CATEGORIES["missing_section"]
    assert pat.search("missing section: 보안")
    assert pat.search("누락된 필수 섹션 발견")


def test_page_count_mismatch_매치():
    pat = GOTCHA_CATEGORIES["page_count_mismatch"]
    assert pat.search("47개 vs 50장 불일치")


def test_build_hints_임계_미만_제외():
    hints = build_gotcha_hints({"missing_section": 1}, min_occurrences=2)
    assert hints == ""  # 1회 < 2


def test_build_hints_상위_5개만():
    counts = {f"missing_section": 10}
    counts.update({f"forbidden_word_repeat": 9})
    hints = build_gotcha_hints(counts, min_occurrences=1)
    assert "missing_section" not in hints  # 카테고리명이 아니라 hint 본문
    assert "필수 섹션" in hints
    assert "TBD" in hints or "placeholder" in hints


def test_build_hints_빈입력():
    assert build_gotcha_hints({}) == ""
    assert build_gotcha_hints(None or {}) == ""
