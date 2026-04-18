"""
Stage 25: 소스 기반 시각 휴리스틱 (Playwright 없이도 작동).

렌더링 없이 HTML 소스에서 시각 문제 가능성을 추정. Playwright 가 있으면
renderer.py 가 더 정확한 픽셀 검증을 추가로 수행.
"""
from __future__ import annotations

import re
from typing import Any


def analyze_html_source(html: str) -> list[str]:
    """HTML 소스만 보고 시각 문제 "가능성" 을 감지.

    반환: issue 키 목록 (예: ["unstyled_sections", "position_absolute_overuse"])
    """
    issues: list[str] = []
    if not html or "<" not in html:
        return issues

    total_len = len(html)

    # 1. Unstyled 가능성 — class 많이 쓰지만 style 블록 없거나 작음
    classes = re.findall(r'class=["\']([^"\']+)["\']', html)
    class_count = sum(len(c.split()) for c in classes)
    style_blocks = re.findall(r"<style[^>]*>([\s\S]*?)</style>", html)
    style_total = sum(len(s) for s in style_blocks)
    if class_count > 10 and style_total < 200:
        issues.append("likely_unstyled_classes")

    # 2. position:absolute 남용 — 잘못된 레이아웃 가능성
    absolute_count = len(re.findall(r"position\s*:\s*absolute", html))
    if absolute_count > 20:
        issues.append("absolute_overuse")

    # 3. z-index 높은 값 여러 개 — 겹침 가능성
    high_z = [int(m) for m in re.findall(r"z-index\s*:\s*(\d+)", html)
              if int(m) > 100]
    if len(high_z) >= 3:
        issues.append("z_index_war")

    # 4. 긴 텍스트 블록이 inline style 없이 — overflow 가능성
    paras = re.findall(r"<p[^>]*>([\s\S]{500,})</p>", html)
    if len(paras) >= 5:
        issues.append("long_text_overflow_risk")

    # 5. 반응형 미지원 — viewport meta 없음
    if "<meta" in html and "viewport" not in html:
        issues.append("no_viewport_meta")

    # 6. 이미지 width 미지정 — 레이아웃 시프트 가능성
    imgs_without_dim = re.findall(
        r'<img(?![^>]*(?:width|style=[^>]*width))[^>]*>', html,
    )
    if len(imgs_without_dim) >= 5:
        issues.append("images_without_dimensions")

    # 7. 색상 대비 명시 — 너무 옅은 텍스트 감지 (color: #XXX 가 배경과 비슷)
    body_bg_match = re.search(
        r"body[^{]*\{[^}]*background[^;]*:\s*(#[\dA-Fa-f]+)", html,
    )
    if body_bg_match:
        bg = body_bg_match.group(1).lower()
        # 간단 체크: bg 가 #11xxxx (dark) 인데 text 가 #77xxxx (어둑) 이면 의심
        muted_texts = re.findall(r"color\s*:\s*(#[\dA-Fa-f]{3,6})", html)
        if any(_is_low_contrast(bg, t) for t in muted_texts):
            issues.append("low_contrast_text")

    # 8. 섹션 내용 너무 적음 — placeholder 의심
    sections = re.findall(
        r"<section[^>]*>([\s\S]*?)</section>", html,
    )
    placeholder_count = sum(1 for s in sections if len(s.strip()) < 200)
    if placeholder_count > 0:
        issues.append(f"short_sections:{placeholder_count}")

    # 9. 빈 버튼·링크
    empty_buttons = re.findall(r"<button[^>]*>\s*</button>", html)
    empty_links = re.findall(r"<a[^>]*>\s*</a>", html)
    if len(empty_buttons) + len(empty_links) >= 3:
        issues.append("empty_interactive_elements")

    return issues


def _is_low_contrast(bg_hex: str, fg_hex: str) -> bool:
    """간단한 contrast 체크 — 실제 WCAG 계산 대신 색 거리 근사."""
    def _rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return (128, 128, 128)
        try:
            return int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except ValueError:
            return (128, 128, 128)

    r1, g1, b1 = _rgb(bg_hex)
    r2, g2, b2 = _rgb(fg_hex)
    # Euclidean distance
    dist = ((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2) ** 0.5
    return dist < 80  # 임계 — 너무 유사
