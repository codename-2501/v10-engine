"""F4 chunked-document 핵심 로직 단위 테스트 — S1-5.

executor._chunked_document_generate 는 외부 의존(LLM·DB) 때문에 통합 테스트가
어렵지만, 내부에 임베드된 ID 추출 패턴과 outline 응답 파서는 순수 함수 — 패턴
정확성을 별도로 검증해 회귀 방지.

핵심 회귀 포인트:
- SCR-XXX / SC-AU-XXX / API-XXX / FR-XXX 등 ID 패턴 매칭
- Korean-aware boundary (한글 접촉 허용, ASCII 인접만 차단)
- outline 응답 파싱 ("ID | 이름" 라인 추출)

실행: pytest tests/test_chunked_document.py -v
"""
from __future__ import annotations

import re

# executor.py 와 동일한 ID 패턴 (변경 시 함께 갱신).
_ID_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9_])SCR-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])SC-[A-Z]{2,4}-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])FR-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])UC-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])RSK-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])KPI-\d{3,4}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])API-\d{3,4}(?![A-Za-z0-9_])"),
]


def extract_ids(text: str) -> set[str]:
    out: set[str] = set()
    for p in _ID_PATTERNS:
        out.update(p.findall(text))
    return out


# ---------------------------------------------------------------------------
# ID 추출 — 표준 케이스
# ---------------------------------------------------------------------------

def test_SCR_표준():
    assert "SCR-001" in extract_ids("화면 SCR-001 설명")


def test_SC_그룹_접두():
    """실버케어 등 서브시스템별 그룹 (SC-AU-001, SC-CW-001)."""
    ids = extract_ids("로그인은 SC-AU-001, 고객 홈은 SC-CW-001")
    assert "SC-AU-001" in ids
    assert "SC-CW-001" in ids


def test_API_FR_UC_RSK_KPI():
    text = "API-001, FR-002, UC-003, RSK-004, KPI-005"
    ids = extract_ids(text)
    assert {"API-001", "FR-002", "UC-003", "RSK-004", "KPI-005"} <= ids


def test_4자리_숫자():
    """3~4자리 모두 허용."""
    assert "SCR-1234" in extract_ids("SCR-1234")


# ---------------------------------------------------------------------------
# Korean-aware boundary
# ---------------------------------------------------------------------------

def test_한국어_조사_접촉():
    """SCR-001은 / SCR-001을 — 한국어 조사 접촉 시에도 매치."""
    for suffix in ("은", "는", "을", "를", "이", "가", "에서", "로"):
        ids = extract_ids(f"화면 SCR-001{suffix} 처리")
        assert "SCR-001" in ids, f"SCR-001{suffix} 미매치"


def test_ASCII_인접_차단():
    """SCR-001A 같은 식별자 일부는 매치 X (boundary)."""
    assert "SCR-001" not in extract_ids("SCR-001A")
    assert "SCR-001" not in extract_ids("XSCR-001")
    assert "SCR-001" not in extract_ids("SCR-0011")


# ---------------------------------------------------------------------------
# Outline 응답 파서 — "ID | 이름" 라인
# ---------------------------------------------------------------------------

OUTLINE_LINE_RE = re.compile(
    r"^\s*([A-Z]{2,4}(?:-[A-Z]{2,4})?-\d{3,4})\s*[|│]\s*([^\n]+?)\s*$",
    re.MULTILINE,
)


def _parse_outline(text: str) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2).strip()
            for m in OUTLINE_LINE_RE.finditer(text)}


def test_outline_표준_포맷():
    src = """SC-AU-001 | 로그인
SC-AU-002 | 회원가입
SC-CW-001 | 고객 홈"""
    out = _parse_outline(src)
    assert out["SC-AU-001"] == "로그인"
    assert out["SC-AU-002"] == "회원가입"
    assert out["SC-CW-001"] == "고객 홈"


def test_outline_전각_파이프():
    """일부 LLM은 전각 │ 사용."""
    src = "API-001 │ 로그인 API"
    out = _parse_outline(src)
    assert out["API-001"] == "로그인 API"


def test_outline_공백_여유():
    src = "  FR-001  |  사용자 등록  "
    out = _parse_outline(src)
    assert out["FR-001"] == "사용자 등록"


def test_outline_비ID_라인_무시():
    src = """## 화면 목록
SC-AU-001 | 로그인
설명: 첫 화면
SC-AU-002 | 회원가입"""
    out = _parse_outline(src)
    # 헤더·설명 라인은 무시되고 ID 2개만 파싱
    assert len(out) == 2
    assert "SC-AU-001" in out
    assert "SC-AU-002" in out
