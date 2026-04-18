"""
engine/skills/design_html.py
Design HTML Reference Injection — preview/ 폴더의 시안 HTML을 AI 프롬프트에 주입.

BUILD 프론트엔드 노드에서 AI 폴백 경로 사용 시,
시안(preview/*.html)을 참조로 포함하여 디자인 1:1 매칭을 유도한다.
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 시안 HTML 최대 크기 (문자 기준) — 너무 크면 토큰 예산 초과 방지
MAX_DESIGN_HTML_CHARS = 80_000


def _slugify(name: str) -> str:
    """파일명/페이지명 → 비교용 슬러그 (소문자, 특수문자 제거)."""
    s = re.sub(r'[^a-z0-9가-힣]', '', name.lower())
    return s


def _match_design_html(
    page_slugs: list[str],
    preview_files: dict[str, str],
) -> dict[str, str]:
    """페이지 슬러그와 preview HTML 파일 매칭.

    매칭 전략:
      1. 정확 매칭 (slug == filename stem)
      2. 포함 매칭 (slug in filename 또는 filename in slug)
      3. difflib 유사도 (0.6 이상)

    Returns:
        {page_slug: html_content} 매칭된 것만
    """
    result: dict[str, str] = {}
    preview_stems = {Path(f).stem: f for f in preview_files}

    for slug in page_slugs:
        slug_norm = _slugify(slug)

        # 1. 정확 매칭
        if slug in preview_stems:
            result[slug] = preview_files[preview_stems[slug]]
            continue

        # 2. 포함 매칭
        matched = False
        for stem, fname in preview_stems.items():
            stem_norm = _slugify(stem)
            if slug_norm and stem_norm and (slug_norm in stem_norm or stem_norm in slug_norm):
                result[slug] = preview_files[fname]
                matched = True
                break

        if matched:
            continue

        # 3. difflib 유사도
        best_ratio = 0.0
        best_file = None
        for stem, fname in preview_stems.items():
            ratio = difflib.SequenceMatcher(None, slug_norm, _slugify(stem)).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_file = fname
        if best_ratio >= 0.6 and best_file:
            result[slug] = preview_files[best_file]

    return result


async def load_design_htmls_for_prompt(
    db: Any,
    project_id: str,
    page_slugs: list[str] | None = None,
) -> str:
    """preview/ 폴더에서 시안 HTML을 읽어 프롬프트 블록으로 반환.

    Args:
        db: DB connection (workspace path 해석에 사용)
        project_id: 프로젝트 ID
        page_slugs: 생성 대상 페이지 슬러그 목록 (None이면 전체)

    Returns:
        프롬프트에 추가할 시안 참조 텍스트. 없으면 빈 문자열.
    """
    from engine.skills.artifact.loader import _resolve_workspace_path_for_project

    workspace_path = await _resolve_workspace_path_for_project(db, project_id)
    if not workspace_path:
        logger.debug("design_html: no workspace path for project=%s", project_id)
        return ""

    preview_dir = workspace_path / "preview"
    if not preview_dir.is_dir():
        logger.debug("design_html: no preview dir at %s", preview_dir)
        return ""

    # preview/*.html 로드
    preview_files: dict[str, str] = {}
    for html_file in sorted(preview_dir.glob("*.html")):
        try:
            content = html_file.read_text("utf-8")
            if content.strip():
                preview_files[html_file.name] = content
        except Exception as e:
            logger.warning("design_html: failed to read %s: %s", html_file, e)

    if not preview_files:
        logger.debug("design_html: no HTML files in %s", preview_dir)
        return ""

    logger.info(
        "design_html: found %d preview files in %s",
        len(preview_files), preview_dir,
    )

    # 매칭
    if page_slugs:
        matched = _match_design_html(page_slugs, preview_files)
    else:
        # 슬러그 없으면 전체 시안 포함 (크기 제한 적용)
        matched = {Path(f).stem: content for f, content in preview_files.items()}

    if not matched:
        logger.debug("design_html: no matches for slugs=%s", page_slugs)
        return ""

    # 프롬프트 블록 조립
    parts: list[str] = [
        "\n\n## 시안 참조 (이 디자인과 1:1로 매칭해야 함)\n"
        "아래는 각 페이지의 디자인 시안 HTML입니다. "
        "레이아웃, 색상, 폰트, 간격, 컴포넌트 구조를 그대로 재현하십시오.\n"
    ]

    total_chars = 0
    for slug, html_content in matched.items():
        # 크기 제한 체크
        if total_chars + len(html_content) > MAX_DESIGN_HTML_CHARS:
            remaining = MAX_DESIGN_HTML_CHARS - total_chars
            if remaining > 1000:
                html_content = html_content[:remaining] + "\n<!-- ... 시안 크기 제한으로 절단됨 -->"
            else:
                parts.append(f"\n### {slug}\n(시안 크기 제한 초과 — 생략)")
                continue

        parts.append(f"\n### {slug}\n```html\n{html_content}\n```")
        total_chars += len(html_content)

    prompt_block = "\n".join(parts)

    logger.info(
        "design_html: injected %d pages, %d chars",
        len(matched), len(prompt_block),
    )

    return prompt_block
