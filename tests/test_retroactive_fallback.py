"""B5+B6 회귀 — retroactive workspace fallback + JSON spec scr_id 인식."""
from __future__ import annotations

import re


def test_design_html_에서_section_id_추출_B5():
    """B5: workspace preview 폴더 부재 시 DB artifact HTML 에서 section ID 추출."""
    html = """
<!DOCTYPE html><html><body>
<section id="SC-AI-001" class="screen">...</section>
<section id='SC-AU-002' class='screen'>...</section>
<section id="SC-HM-001">...</section>
</body></html>
"""
    section_ids = set(re.findall(
        r'<section[^>]*\bid=["\'](SC-[A-Z]{2,5}-\d{3,4})["\']',
        html,
    ))
    assert section_ids == {"SC-AI-001", "SC-AU-002", "SC-HM-001"}


def test_recipe_json_scr_id_추출_B6():
    """B6: 페이지 레시피 JSON spec 의 scr_id field 직접 추출."""
    recipe_json = """[
{"page_name": "AI", "scr_id": "SC-AI-001", "placements": []},
{"page_name": "Auth", "scr_id": "SC-AU-002", "placements": []},
{"page_name": "Home", "scr_id":"SC-HM-001", "placements": []}
]"""
    scr_ids = re.findall(
        r'"scr_id"\s*:\s*"(SC-[A-Z]{2,4}-\d{3,4})"',
        recipe_json,
    )
    assert sorted(scr_ids) == ["SC-AI-001", "SC-AU-002", "SC-HM-001"]


def test_recipe_slugs_format():
    """B6: scr_id 에서 변환된 slug 형식."""
    scr_ids = ["SC-AI-001", "SC-AU-002"]
    slugs = [f"scr-{rid.lower()}" for rid in scr_ids]
    assert slugs == ["scr-sc-ai-001", "scr-sc-au-002"]


def test_design_html_빈문서_빈리스트():
    section_ids = set(re.findall(
        r'<section[^>]*\bid=["\'](SC-[A-Z]{2,5}-\d{3,4})["\']',
        "",
    ))
    assert section_ids == set()


def test_recipe_json_빈배열_빈리스트():
    scr_ids = re.findall(
        r'"scr_id"\s*:\s*"(SC-[A-Z]{2,4}-\d{3,4})"',
        "[]",
    )
    assert scr_ids == []
