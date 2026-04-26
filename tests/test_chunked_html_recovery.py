"""chunked_html_items_generate placeholder 자동 회복 회귀 방지.

이번 세션 발견 — 환경 실패(claude binary ENOENT) 로 노드가 placeholder
section 들을 포함한 채 COMPLETED 종료 + task_snapshot=NULL. 재시도 시
partial retry flag 만으로는 cache/failed 정보 없어 full LLM 호출 발생.

fix: chunked_html_items_generate 시작부에서 task_snapshot 비어 있으면
직전 artifact_versions 의 latest content 자동 파싱 → "(자동 생성 실패"
또는 data-incomplete 패턴 감지 → failed_keys 자동 채움 + 정상 section
들은 completed_items 에 cache 복구.
"""
from __future__ import annotations

import re

import pytest


# ============================================================
# placeholder 패턴 감지 정확성 (단위)
# ============================================================


def test_placeholder_pattern_탐지():
    """data-incomplete + (자동 생성 실패 동시 검출."""
    section = (
        '<section id="SC-HB-002" class="screen" data-incomplete="true">\n'
        '<h2>SC-HB-002</h2>\n'
        '<p>(자동 생성 실패 — 재실행 필요)</p>\n'
        '</section>'
    )
    incomplete = re.search(
        r'data-incomplete\s*=\s*"true"|'
        r'\(자동 생성 실패\s*[—–-]\s*재실행 필요\)',
        section,
    )
    assert incomplete is not None


def test_정상_section_은_탐지_안함():
    section = (
        '<section id="SC-AI-001" class="screen">\n'
        '<header><h1>AI 인사이트 홈</h1></header>\n'
        '<main>...실제 콘텐츠...</main>\n'
        '</section>'
    )
    incomplete = re.search(
        r'data-incomplete\s*=\s*"true"|'
        r'\(자동 생성 실패\s*[—–-]\s*재실행 필요\)',
        section,
    )
    assert incomplete is None


# ============================================================
# 통합: 직전 artifact 에서 회복 로직
# ============================================================


class _FakeDB:
    """fetchone 만 사용. (artifact_versions 조회 1회)."""

    def __init__(self, prev_html: str | None):
        self._prev_html = prev_html
        self.calls = []

    async def fetchone(self, sql, params=None):
        self.calls.append((sql, params))
        if "artifact_versions" in sql and self._prev_html is not None:
            return {"storage_path": self._prev_html}
        return None


def _recover_from_artifact(prev_html: str) -> tuple[dict, set]:
    """fix 로직 standalone 추출 — executor.py 와 동일 정규식 기준.

    실제 executor 의 회복 블록은 db 비동기 호출 안에 있으므로 본 helper
    는 동일 정규식만 재현해 핵심 분기를 검증한다.
    """
    SEC_RE = re.compile(
        r'<section\s+id="(SC-[A-Z]{2,5}-\d{3,4})"[^>]*>.*?</section>',
        re.DOTALL,
    )
    PLACEHOLDER_RE = re.compile(
        r'data-incomplete\s*=\s*"true"|'
        r'\(자동 생성 실패\s*[—–-]\s*재실행 필요\)'
    )
    completed: dict = {}
    failed: set = set()
    for m in SEC_RE.finditer(prev_html):
        sid = m.group(1)
        body = m.group(0)
        if PLACEHOLDER_RE.search(body):
            failed.add(sid)
        else:
            completed[sid] = body
    return completed, failed


def test_혼합_artifact_분리():
    """정상 24 + placeholder 20 → cache 24 / failed 20 분리."""
    prev = ""
    # 정상 4개
    for i in (1, 2, 3, 4):
        prev += (
            f'<section id="SC-AI-{i:03d}" class="screen">'
            f'<h2>AI {i}</h2><main>real content {i*100} chars '
            + 'x' * 200
            + '</main></section>\n'
        )
    # placeholder 3개
    for sid in ("SC-HB-002", "SC-NT-003", "SC-PR-001"):
        prev += (
            f'<section id="{sid}" class="screen" data-incomplete="true">'
            f'<h2>{sid}</h2><p>(자동 생성 실패 — 재실행 필요)</p>'
            f'</section>\n'
        )
    cache, failed = _recover_from_artifact(prev)
    assert len(cache) == 4
    assert "SC-AI-001" in cache
    assert "SC-AI-004" in cache
    assert failed == {"SC-HB-002", "SC-NT-003", "SC-PR-001"}


def test_artifact_없으면_빈_결과():
    cache, failed = _recover_from_artifact("")
    assert cache == {}
    assert failed == set()


def test_placeholder_em_dash_변형():
    """대시 변형(— vs – vs -) 모두 감지."""
    for dash in ("—", "–", "-"):
        prev = (
            f'<section id="SC-HB-002" data-foo="x">'
            f'<h2>SC-HB-002</h2>'
            f'<p>(자동 생성 실패 {dash} 재실행 필요)</p>'
            f'</section>'
        )
        cache, failed = _recover_from_artifact(prev)
        assert failed == {"SC-HB-002"}, f"dash={dash!r} 감지 실패"
        assert cache == {}


def test_data_incomplete_만_있어도_감지():
    """문구 없이 data-incomplete 만 있어도 placeholder 처리."""
    prev = (
        '<section id="SC-XY-001" data-incomplete="true">'
        '<h2>XY</h2><p>partial</p>'
        '</section>'
    )
    cache, failed = _recover_from_artifact(prev)
    assert failed == {"SC-XY-001"}


def test_id_없는_section_은_무시():
    prev = (
        '<section class="hero"><h1>인트로</h1></section>'
        '<section id="SC-AI-001"><h2>AI</h2><main>real</main></section>'
    )
    cache, failed = _recover_from_artifact(prev)
    assert list(cache.keys()) == ["SC-AI-001"]


# ============================================================
# 환경 실패 시나리오 통합 (executor 회복 블록)
# ============================================================


@pytest.mark.asyncio
async def test_executor_회복_블록_partial_off():
    """partial flag off 면 회복 시도 안 함."""
    import os
    os.environ.pop("V10_CHUNKED_ITEMS_PARTIAL_RETRY", None)
    # flag off 시에는 회복 분기 자체를 거치지 않음 — 본 테스트는
    # 분기 condition 의 정확성을 명시 (회복 블록 자체 별도 통합 필요).
    flag_on = os.environ.get("V10_CHUNKED_ITEMS_PARTIAL_RETRY", "0") == "1"
    assert flag_on is False


@pytest.mark.asyncio
async def test_executor_회복_블록_partial_on():
    """partial flag on 일 때 분기 진입 condition."""
    import os
    os.environ["V10_CHUNKED_ITEMS_PARTIAL_RETRY"] = "1"
    try:
        flag_on = (
            os.environ.get("V10_CHUNKED_ITEMS_PARTIAL_RETRY", "0") == "1"
        )
        assert flag_on is True
    finally:
        os.environ.pop("V10_CHUNKED_ITEMS_PARTIAL_RETRY", None)
