"""Stage 25 visual heuristics 테스트 (Playwright 없이)."""
from __future__ import annotations

from engine.skills.visual.heuristics import analyze_html_source


def test_no_issues_on_valid_html():
    html = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">
<style>body { color: #eee; background: #111; } .card { padding: 16px; }</style>
</head><body>
<section><h1>Hello</h1><p>Short text</p></section>
</body></html>
"""
    issues = analyze_html_source(html)
    # viewport 있음 + 클래스 대비 style 충분
    assert "no_viewport_meta" not in issues
    assert "likely_unstyled_classes" not in issues


def test_unstyled_classes_detected():
    html = '<div class="a b c d e f g h i j k"></div>'
    issues = analyze_html_source(html)
    assert "likely_unstyled_classes" in issues


def test_no_viewport_meta():
    html = "<head><meta charset='utf-8'></head>"
    issues = analyze_html_source(html)
    assert "no_viewport_meta" in issues


def test_absolute_overuse():
    html = " ".join([f'<div style="position: absolute">x</div>' for _ in range(25)])
    issues = analyze_html_source(html)
    assert "absolute_overuse" in issues


def test_z_index_war():
    html = "\n".join([
        "<div style='z-index: 200'></div>",
        "<div style='z-index: 999'></div>",
        "<div style='z-index: 500'></div>",
    ])
    issues = analyze_html_source(html)
    assert "z_index_war" in issues


def test_short_sections_placeholder_risk():
    html = "<section>x</section><section>y</section>"
    issues = analyze_html_source(html)
    assert any(i.startswith("short_sections") for i in issues)


def test_empty_buttons():
    html = "<button></button><button></button><button></button><a></a>"
    issues = analyze_html_source(html)
    assert "empty_interactive_elements" in issues


def test_low_contrast():
    html = """
<style>body { background: #111111; } p { color: #222222; }</style>
<p>hard to read</p>
"""
    issues = analyze_html_source(html)
    assert "low_contrast_text" in issues
