"""S9: _resolve_chunk_items 범용 로직 단위 테스트."""
from __future__ import annotations

import pytest

from engine.skills.executor import _resolve_chunk_items


class _FakeDB:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self, sql, params=None):
        return self._row


class _FakeNode:
    def __init__(self, name="X", project_id="p1", id_="n1"):
        self.name = name
        self.project_id = project_id
        self.id = id_


# ---------------------------------------------------------------------------
# 우선순위 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_명시_chunk_items_우선():
    spec = {"chunk_items": ["a", "b", "c"]}
    r = await _resolve_chunk_items(spec, _FakeNode(), None)
    assert r == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_upstream_artifact_추출_실버케어():
    """실버케어 SC-AU/CW 그룹 ID 추출."""
    art_content = """
    # 화면 목록 정의서

    | ID | 이름 |
    |---|---|
    | SC-AU-001 | 로그인 |
    | SC-AU-002 | 회원가입 |
    | SC-CW-001 | 고객 홈 |
    | SC-CW-002 | 예약 |
    """
    db = _FakeDB(row={"content": art_content})
    spec = {
        "chunk_items_source": {
            "from_spec": "화면 목록 정의서",
            "extract_pattern": r"(SC-[A-Z]{2,4}-\d{3,4}|SCR-\d{3,4})",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r == ["SC-AU-001", "SC-AU-002", "SC-CW-001", "SC-CW-002"]


@pytest.mark.asyncio
async def test_upstream_추출_이커머스():
    """이커머스 SC-SH/OD 그룹도 같은 regex 로 추출."""
    art = "SC-SH-001 상품목록\nSC-SH-002 상세\nSC-OD-001 주문\nSC-OD-002 결제"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "화면 목록 정의서",
            "extract_pattern": r"(SC-[A-Z]{2,4}-\d{3,4}|SCR-\d{3,4})",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r == ["SC-SH-001", "SC-SH-002", "SC-OD-001", "SC-OD-002"]


@pytest.mark.asyncio
async def test_upstream_SCR_포맷():
    """SCR-001 단순 포맷도 동작."""
    art = "화면: SCR-001, SCR-002, SCR-003"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "화면 목록",
            "extract_pattern": r"(SC-[A-Z]{2,4}-\d{3,4}|SCR-\d{3,4})",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r == ["SCR-001", "SCR-002", "SCR-003"]


@pytest.mark.asyncio
async def test_중복_제거_순서_유지():
    art = "SC-AU-001 SC-AU-001 SC-AU-002 SC-AU-001 SC-AU-003"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "X",
            "extract_pattern": r"(SC-[A-Z]{2,4}-\d{3,4})",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    # A-2 threshold=3 충족 + 중복 제거
    assert r == ["SC-AU-001", "SC-AU-002", "SC-AU-003"]


@pytest.mark.asyncio
async def test_upstream_없음_None():
    """upstream 아직 완료 안 됐으면 None (단일 호출 fallback)."""
    db = _FakeDB(row=None)
    spec = {
        "chunk_items_source": {
            "from_spec": "없는 문서",
            "extract_pattern": r"(SC-\d+)",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r is None


@pytest.mark.asyncio
async def test_split_categories_fallback():
    """split_categories + 노드 이름 매칭."""
    spec = {
        "split_categories": [
            {"name": "layout", "chunk_items": ["header", "footer"]},
            {"name": "feedback", "chunk_items": ["toast", "modal"]},
        ]
    }
    r = await _resolve_chunk_items(
        spec, _FakeNode(name="컴포넌트 라이브러리 (feedback)"), None,
    )
    assert r == ["toast", "modal"]


@pytest.mark.asyncio
async def test_전부_없음_None():
    """어떤 필드도 없으면 None."""
    r = await _resolve_chunk_items({"name": "x"}, _FakeNode(), None)
    assert r is None


@pytest.mark.asyncio
async def test_regex_매치_0건_None():
    """extract_pattern 은 있으나 content 에 매치 없음 → None."""
    db = _FakeDB(row={"content": "일반 한국어 본문 숫자 없음"})
    spec = {
        "chunk_items_source": {
            "from_spec": "X",
            "extract_pattern": r"(SC-[A-Z]+-\d+)",
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r is None


@pytest.mark.asyncio
async def test_db_예외_fallback():
    """DB 조회 예외 시 None 반환 (크래시 X)."""
    class _ThrowDB:
        async def fetchone(self, sql, params=None):
            raise RuntimeError("db down")
    spec = {
        "chunk_items_source": {"from_spec": "X", "extract_pattern": r"(\d+)"}
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), _ThrowDB())
    assert r is None


# ---------------------------------------------------------------------------
# A-2: extract_patterns fallback chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patterns_첫번째_매치_3개_이상이면_채택():
    """fallback chain: 첫 패턴이 3개 이상 매치하면 채택."""
    art = "SC-AU-001 SC-AU-002 SC-AU-003 FR-001 FR-002"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "X",
            "extract_patterns": [
                r"(SC-[A-Z]{2,4}-\d{3,4})",  # 첫 매치 3개
                r"(FR-\d{3,4})",
            ],
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    # 첫 패턴 3개 채택, FR 무시
    assert r == ["SC-AU-001", "SC-AU-002", "SC-AU-003"]


@pytest.mark.asyncio
async def test_patterns_첫_실패_두번째_채택():
    """첫 패턴 매치 0~2개 → 다음 패턴 시도."""
    art = "FR-001 설명\nFR-002 기능\nFR-003 기능2"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "X",
            "extract_patterns": [
                r"(SC-[A-Z]+-\d+)",  # 0 매치
                r"(FR-\d{3,4})",     # 3 매치
            ],
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r == ["FR-001", "FR-002", "FR-003"]


@pytest.mark.asyncio
async def test_patterns_전부_실패_None():
    art = "일반 텍스트 ID 없음"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {
            "from_spec": "X",
            "extract_patterns": [
                r"(SC-[A-Z]+-\d+)",
                r"(FR-\d+)",
            ],
        }
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    assert r is None


@pytest.mark.asyncio
async def test_기본_fallback_chain_없이도_동작():
    """extract_pattern·patterns 아무것도 없으면 내장 4종 chain 시도."""
    art = "SC-SH-001 SC-SH-002 SC-SH-003 SC-SH-004"
    db = _FakeDB(row={"content": art})
    spec = {
        "chunk_items_source": {"from_spec": "X"},
        # extract_pattern, extract_patterns 둘 다 없음
    }
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    # 내장 첫 패턴 (SC-|SCR-) 이 4개 매치
    assert r is not None
    assert "SC-SH-001" in r
    assert len(r) == 4


@pytest.mark.asyncio
async def test_기본_chain_FR_API_매치():
    """내장 chain: 화면 없고 FR·API 만 있으면 후순위 패턴으로 매치."""
    art = "FR-001 FR-002 FR-003 FR-004 FR-005"
    db = _FakeDB(row={"content": art})
    spec = {"chunk_items_source": {"from_spec": "X"}}
    r = await _resolve_chunk_items(spec, _FakeNode(), db)
    # SC/SCR 0 매치 → FR 으로 fallback
    assert r == ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"]
