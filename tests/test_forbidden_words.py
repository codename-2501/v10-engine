"""harness.py 금지어 정규식 회귀 테스트 — S1-5.

Korean-aware boundary 핵심 케이스:
- '미완성' 같은 한국어 복합어가 false positive 되지 않아야 함 (이전에 발생)
- '미정산'·'미정의' 등 '미정' 부분 매치 금지
- TBD가 한국어 조사와 붙어도 (TBD로) 매치 성공
- ASCII 식별자 안의 TBD (myTBDvar) 는 매치 X

이 테스트는 _harness_validate_document 내부 _forbidden_re 와 동일한 패턴을
직접 컴파일해 검증 (구현 결합 최소화).

실행: pytest tests/test_forbidden_words.py -v
"""
from __future__ import annotations

import re

# harness.py 와 동일 패턴 — 패턴이 변경되면 이 테스트도 같이 갱신해야 함을
# 명시적으로 드러냄 (회귀 가드의 본질).
FORBIDDEN_RE = re.compile(
    r'(?<![a-zA-Z0-9_])(TODO|TBD|FIXME|추후\s*작성|작성\s*예정|추후\s*결정)(?![a-zA-Z0-9_])',
    re.IGNORECASE,
)


def _has_match(text: str) -> bool:
    return FORBIDDEN_RE.search(text) is not None


# ---------------------------------------------------------------------------
# True positive — 반드시 잡혀야 함
# ---------------------------------------------------------------------------

def test_TBD_단독_매치():
    assert _has_match("이 부분은 TBD 입니다")


def test_TBD_조사_접촉_매치():
    """TBD로 / TBD은 / TBD가 — Korean particle 접촉도 매치되어야 함."""
    for s in ("TBD로", "TBD은", "TBD가", "TBD를", "TBD의"):
        assert _has_match(f"값은 {s} 처리"), f"{s} 미매치"


def test_TODO_매치():
    assert _has_match("TODO: 작성")
    assert _has_match("그냥 TODO 입니다")


def test_FIXME_매치():
    assert _has_match("FIXME 수정 필요")


def test_한국어_multi_word_매치():
    assert _has_match("이 부분은 추후 작성 합니다")
    assert _has_match("이 부분은 작성 예정 입니다")
    assert _has_match("정책은 추후 결정 됩니다")


def test_lowercase_매치():
    """대소문자 구분 없음."""
    assert _has_match("값은 tbd 입니다")
    assert _has_match("값은 todo 입니다")


# ---------------------------------------------------------------------------
# False positive — 잡히면 안 됨 (과거 버그)
# ---------------------------------------------------------------------------

def test_미완성은_매치_X():
    """'미완성'은 정상 한국어 명사 — 이전 false positive 사례."""
    assert not _has_match("이 화면은 미완성품을 다룬다")
    assert not _has_match("미완성 상태 처리")


def test_미정산_미정의_매치_X():
    """'미정' 부분 매치로 미정산/미정의가 잡혔던 과거 버그 회귀."""
    assert not _has_match("미정산 거래 내역")
    assert not _has_match("미정의 동작은 에러")


def test_ASCII_식별자_안의_TBD_매치_X():
    """myTBDvar / TBD_FLAG 같은 식별자 보호."""
    assert not _has_match("let myTBDvar = 1")
    assert not _has_match("const TBD_FLAG = true")
    assert not _has_match("STBD123")  # ASCII alphanumeric 인접


def test_정상_본문_매치_X():
    assert not _has_match("정상적인 한국어 본문 내용입니다.")
    assert not _has_match("API 응답 시간은 200ms 미만이어야 한다.")
