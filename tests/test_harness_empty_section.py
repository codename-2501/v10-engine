"""F1 — _harness_validate_document 의 빈섹션 본문 검증.

required_sections 헤딩만 있고 본문이 비거나 부실한 경우 catch 검증.
spec 의 structural_rules.min_section_body 필드로 임계값 설정.
"""

from engine.skills.qa.harness import (
    _harness_validate_document, _extract_section_body,
)


def _spec(required_sections, min_section_body=100):
    return {
        "validation": {
            "structural": {
                "required_sections": required_sections,
                "min_section_body": min_section_body,
            }
        }
    }


def test_empty_section_caught():
    """헤딩 있고 본문 0자 — FAIL."""
    content = """# PRD

## 사용자

## 기능

## 비기능
"""
    result = _harness_validate_document(content, "PRD", _spec(["사용자", "기능", "비기능"]))
    assert not result["pass"]
    assert any("빈/부실 섹션" in f for f in result["structural_failures"])


def test_short_section_caught():
    """헤딩 있고 본문 부족 (< min_section_body) — FAIL."""
    content = """# PRD

## 사용자
짧음.

## 기능
""" + "X" * 200 + """

## 비기능
""" + "Y" * 200
    result = _harness_validate_document(content, "PRD", _spec(["사용자", "기능", "비기능"], min_section_body=100))
    assert not result["pass"]
    failures = " ".join(result["structural_failures"])
    assert "사용자" in failures


def test_full_sections_pass():
    """모든 섹션 충실 — PASS (other checks 통과 가정)."""
    content = """# PRD

## 사용자
""" + "U" * 150 + """

## 기능
""" + "F" * 150 + """

## 비기능
""" + "N" * 150 + """

SELF_CHECK: ok
"""
    result = _harness_validate_document(content, "PRD", _spec(["사용자", "기능", "비기능"], min_section_body=100))
    assert result["pass"], f"unexpected fail: {result['structural_failures']}"


def test_min_section_body_zero_skips_check():
    """min_section_body=0 (또는 spec 미정의) → 본문 길이 검사 스킵 — 기존 동작."""
    content = """# PRD

## 사용자

## 기능
"""
    result = _harness_validate_document(content, "PRD", _spec(["사용자", "기능"], min_section_body=0))
    # 다른 항목 (헤딩 존재 등) 만 체크 — 빈섹션은 스킵
    assert "빈/부실 섹션" not in " ".join(result["structural_failures"])


def test_extract_section_body_basic():
    content = """# Title

## A
A 본문 첫 줄
A 본문 두번째 줄

## B
B 본문
"""
    body_a = _extract_section_body(content, "A")
    assert "A 본문" in body_a
    assert "B 본문" not in body_a


def test_extract_section_body_nested():
    """하위 헤딩은 같은 섹션의 본문으로 포함."""
    content = """# Title

## A
A 시작

### A-1
하위 1

### A-2
하위 2

## B
B 본문
"""
    body_a = _extract_section_body(content, "A")
    assert "A 시작" in body_a
    assert "하위 1" in body_a
    assert "B 본문" not in body_a


def test_extract_section_fuzzy_match():
    """공백/대소문자 무시 매칭."""
    content = """## User Personas
content here
""" + "X" * 100
    body = _extract_section_body(content, "user personas")
    assert "content here" in body


def test_missing_section_still_caught():
    """헤딩 자체가 누락 — 기존 missing 검사 그대로."""
    content = """# PRD

## 사용자
""" + "U" * 200
    result = _harness_validate_document(content, "PRD", _spec(["사용자", "기능"], min_section_body=100))
    assert not result["pass"]
    assert any("필수 섹션 누락" in f for f in result["structural_failures"])
