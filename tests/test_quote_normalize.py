"""B3' 회귀 — section 시작 태그 quote normalize. inline JS literal 보호."""
from __future__ import annotations

import re


# B3' 의 정확한 정규식 (executor.py 와 동일 — duplicating for unit test)
_RE_ID_SQ = re.compile(r"<section\b([^>]*?)\bid='([^']+)'")
_RE_CLASS_SQ = re.compile(r"<section\b([^>]*?)\bclass='([^']+)'")


def normalize(html: str) -> str:
    html = _RE_ID_SQ.sub(r'<section\1id="\2"', html)
    html = _RE_CLASS_SQ.sub(r'<section\1class="\2"', html)
    return html


def test_single_quote_id_double_변환():
    html = "<section id='SC-AI-001' class='screen'>...</section>"
    result = normalize(html)
    assert 'id="SC-AI-001"' in result
    assert 'class="screen"' in result
    assert "id='SC-AI-001'" not in result


def test_double_quote_그대로_보존():
    html = '<section id="SC-AI-001" class="screen">...</section>'
    result = normalize(html)
    assert result == html


def test_inline_JS_single_quote_보호():
    """section 시작 태그 안의 inline JS literal 은 변환 안 됨."""
    html = """<section id='SC-AI-001'>
<button onclick="alert('hi')">버튼</button>
<script>const msg = 'hello';</script>
</section>"""
    result = normalize(html)
    assert 'id="SC-AI-001"' in result
    assert "alert('hi')" in result  # inline JS literal 보존
    assert "const msg = 'hello'" in result  # script literal 보존


def test_여러_section_모두_변환():
    html = (
        "<section id='SC-AI-001' class='screen'>...</section>"
        "<section id='SC-AU-002' class='auth'>...</section>"
    )
    result = normalize(html)
    assert 'id="SC-AI-001"' in result
    assert 'id="SC-AU-002"' in result
    assert "id='" not in result


def test_혼합_quote_정규화():
    html = '<section id="SC-AI-001" class=\'screen\' data-x="y">...</section>'
    result = normalize(html)
    assert 'id="SC-AI-001"' in result
    assert 'class="screen"' in result
    assert 'data-x="y"' in result
