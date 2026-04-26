"""B1' 회귀 — main_content_density 룰 + 도메인 prefix config."""
from __future__ import annotations

from engine.skills.qa.harness import (
    _harness_validate_main_content_density,
)


def _section(sid: str, body: str) -> str:
    return f'<section id="{sid}" class="screen">{body}</section>'


def test_dense_section_pass():
    body = "<header><h1>풀 콘텐츠</h1></header>" + "<p>" + "ㅎ" * 600 + "</p>"
    html = _section("SC-AI-001", body)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=["AI"],
    )
    # nodes 가 적어도 visible_chars 충분 — 그러나 둘 다 만족해야 정상이라 fail
    # 본 테스트는 visible 만족 + nodes 부족 → fail (AI prefix)
    assert r["pass"] is False
    assert any(f["id"] == "SC-AI-001" for f in r["failures"])


def test_dense_section_with_nodes_pass():
    inner = "<div>" + "<p>ㅎ</p>" * 25 + "</div>"
    html = _section("SC-AI-001", inner)
    r = _harness_validate_main_content_density(
        html, threshold_text=10, threshold_nodes=20,
        fail_prefixes=["AI"],
    )
    assert r["pass"] is True


def test_layout_shell_AI_prefix_fail():
    body = "<header><h1>AI 인사이트</h1></header><main></main>"
    html = _section("SC-AI-001", body)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=["AI"],
    )
    assert r["pass"] is False
    assert r["failures"][0]["id"] == "SC-AI-001"
    assert r["failures"][0]["level"] == "fail"


def test_layout_shell_non_dashboard_warn():
    body = "<header><h1>로그인</h1></header><main></main>"
    html = _section("SC-AU-001", body)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=["AI", "HM"],  # AU 는 fail 대상 아님
    )
    # warn level — pass=True
    assert r["pass"] is True
    assert any(w["id"] == "SC-AU-001" for w in r["warnings"])


def test_fail_prefixes_빈리스트면_모두_warn():
    body = "<main></main>"
    html = _section("SC-AI-001", body)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=[],
    )
    assert r["pass"] is True
    assert any(w["id"] == "SC-AI-001" for w in r["warnings"])


def test_fail_prefixes_None_도_warn_only():
    body = "<main></main>"
    html = _section("SC-AI-001", body)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=None,
    )
    assert r["pass"] is True
    assert r["warnings"]


def test_여러_section_혼합():
    parts = [
        _section("SC-AI-001", "<main></main>"),  # fail
        _section("SC-AI-002", "<main>" + "x" * 600 + "</main>"
                              + "<div></div>" * 25),  # pass
        _section("SC-AU-001", "<main></main>"),  # warn (AU 는 fail prefix 아님)
    ]
    html = "\n".join(parts)
    r = _harness_validate_main_content_density(
        html, threshold_text=500, threshold_nodes=20,
        fail_prefixes=["AI"],
    )
    assert r["pass"] is False
    assert {f["id"] for f in r["failures"]} == {"SC-AI-001"}
    assert any(w["id"] == "SC-AU-001" for w in r["warnings"])
    assert r["checked_sections"] == 3
