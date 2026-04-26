"""프로젝트 내 이전 HTML 산출물에서 디자인 토큰 블록 추출.

같은 프로젝트에서 이미 생성된 HTML 산출물 (UI 시안 등) 이 있으면 그 `:root`
CSS 변수 블록을 다음 HTML 스킬 프롬프트에 주입해 색/폰트/간격 일관성을 강제.

해결 대상 버그: LLM 이 이전 산출물 테마 참조 없이 독립 생성 → `--bg` 미정의
→ 브라우저 기본값 흰 배경으로 렌더링 (화면 설계서 케이스).
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_ROOT_BLOCK_RE = re.compile(
    r":root\s*\{([^}]+)\}",
    re.IGNORECASE | re.DOTALL,
)


async def extract_project_design_tokens(db: Any, project_id: str) -> str | None:
    """프로젝트의 가장 최신 HTML 산출물에서 `:root { ... }` 블록 추출.

    Returns:
        `:root { --bg: #xxx; ... }` 문자열. 토큰 없으면 None.
    """
    rows = await db.fetchall(
        """
        SELECT av.storage_path
        FROM artifacts a
        JOIN artifact_versions av
          ON av.artifact_id = a.id
         AND av.version_num = a.current_version
        WHERE a.project_id = ?
          AND a.artifact_type = 'html'
        ORDER BY a.updated_at DESC
        LIMIT 5
        """,
        (project_id,),
    )
    for row in rows or []:
        content = row["storage_path"] if row else None
        if not content:
            continue
        m = _ROOT_BLOCK_RE.search(content)
        if not m:
            continue
        body = m.group(1).strip()
        # 의미있는 CSS 변수 최소 2개 이상 있어야 유효 토큰으로 간주
        if body.count("--") < 2:
            continue
        return f":root {{\n{body}\n}}"
    return None


def format_tokens_prompt_block(tokens_css: str | None) -> str:
    """추출한 토큰을 프롬프트에 붙일 수 있는 블록 형태로 포장."""
    if not tokens_css:
        return ""
    return (
        "## 디자인 토큰 승계 (프로젝트 일관성 강제)\n\n"
        "이 프로젝트의 기존 HTML 산출물에서 아래 CSS 변수가 정의되어 있음.\n"
        "새 HTML 의 `<style>` 블록 맨 위에 **그대로 포함**하고 body/섹션 배경은\n"
        "`var(--bg)` 등으로 참조해라. 절대 브라우저 기본 흰색으로 두지 말 것.\n\n"
        "```css\n"
        f"{tokens_css}\n"
        "```\n\n"
        "만약 위 변수 중 필요한 것이 없으면 *추가*는 가능하나, 기존 변수는\n"
        "그대로 유지하여 프로젝트 전체 산출물 색감·톤 통일.\n"
    )
