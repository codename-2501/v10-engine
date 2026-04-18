"""
engine/skills/doc_scaffold.py
기술 문서 스캐폴드 자동 생성 — spec 기반 마크다운 골격.

spec YAML의 required_headings + prompt에서 섹션 구조/테이블 컬럼을 추출하여
마크다운 골격을 프로그래매틱으로 생성. AI는 내용만 채우면 됨.

적용 대상: type=document인 기술 문서 (DEFINE/DESIGN/VERIFY/DELIVER)
제외: type=json/html, BUILD(codegen 처리), 디자인 계열 산출물
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 디자인 계열 산출물 제외 키워드
_DESIGN_EXCLUDE_KEYWORDS = frozenset({
    "컴포넌트", "화면", "디자인 토큰", "레시피", "조립",
    "IA", "시안", "스토리보드", "와이어프레임", "UI 디자인",
    "디자인 시스템", "페이지 조립", "페이지 레시피",
})

# 섹션 + 설명 추출 정규식: "1. **섹션명** — 설명..."
_SECTION_RE = re.compile(r'\d+\.\s+\*\*(.+?)\*\*\s*[—\-–]\s*(.+?)(?=\n\s*\d+\.\s+\*\*|\n\s*###|\Z)', re.DOTALL)

# 테이블 컬럼 추출 정규식: "테이블(컬럼1/컬럼2/컬럼3)" 또는 "표(컬럼1/컬럼2)"
_TABLE_COLS_RE = re.compile(r'(?:테이블|표)\s*[\(（](.+?)[\)）]')


def build_document_scaffold(
    spec: dict | None,
    node_name: str,
    node_phase: str,
) -> str | None:
    """
    spec 기반 마크다운 스캐폴드 생성.

    Returns:
        마크다운 골격 문자열. 적용 불가 시 None (폴백 → 기존 AI 전체 생성).
    """
    if not spec:
        return None

    # 1. 적용 대상 판별
    if spec.get("type", "document") not in ("document",):
        return None

    if node_phase == "BUILD":
        return None  # BUILD는 codegen이 처리

    # 디자인 계열 제외
    if any(kw in node_name for kw in _DESIGN_EXCLUDE_KEYWORDS):
        return None

    # 2. 섹션 목록 추출
    sections = _extract_sections(spec)
    if not sections:
        return None

    # 3. 마크다운 골격 조립
    lines: list[str] = []
    for heading, description, columns in sections:
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"<!-- {description.strip()[:200]} -->")
        lines.append("")

        # 테이블 구조가 있으면 빈 테이블 헤더 추가
        if columns:
            header = "| " + " | ".join(col.strip() for col in columns) + " |"
            separator = "| " + " | ".join("---" for _ in columns) + " |"
            lines.append(header)
            lines.append(separator)
            lines.append("")

    if not lines:
        return None

    scaffold = "\n".join(lines)
    logger.info(
        "doc_scaffold_generated node=%s phase=%s sections=%d chars=%d",
        node_name[:30], node_phase, len(sections), len(scaffold),
    )
    return scaffold


def _extract_sections(spec: dict) -> list[tuple[str, str, list[str] | None]]:
    """
    spec에서 (섹션명, 설명, 테이블컬럼목록|None) 튜플 리스트 추출.

    우선순위:
    1. required_headings + prompt에서 설명/테이블 매칭
    2. prompt에서 **섹션명** — 설명 패턴 직접 파싱
    """
    prompt = spec.get("prompt", "")
    validation = spec.get("validation", {})
    structural = validation.get("structural", {})
    required_headings = structural.get("required_headings", [])

    # prompt에서 섹션별 설명 + 테이블 컬럼 추출
    prompt_sections: dict[str, tuple[str, list[str] | None]] = {}
    for m in _SECTION_RE.finditer(prompt):
        name = m.group(1).strip()
        desc = m.group(2).strip()
        # 테이블 컬럼 추출
        cols_match = _TABLE_COLS_RE.search(desc)
        columns = [c.strip() for c in cols_match.group(1).split("/")] if cols_match else None
        prompt_sections[name] = (desc, columns)

    # 경로 1: required_headings 기반 (가장 신뢰)
    if required_headings:
        result = []
        for heading in required_headings:
            if heading in prompt_sections:
                desc, columns = prompt_sections[heading]
                result.append((heading, desc, columns))
            else:
                result.append((heading, "이 섹션의 내용을 작성하세요", None))
        return result

    # 경로 2: prompt 파싱 결과 사용
    if prompt_sections:
        return [
            (name, desc, cols)
            for name, (desc, cols) in prompt_sections.items()
        ]

    return []
