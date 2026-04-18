"""
Skill Executor utility helpers — small extraction/parsing functions
used throughout the executor module.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        ISO-formatted timestamp, e.g. ``"2026-03-25T12:34:56Z"``.
    """
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_json(content: str) -> Optional[str]:
    """AI 산출물에서 JSON 부분만 추출.

    AI가 ```json ... ``` 코드블록으로 감싸거나
    앞뒤에 설명 텍스트를 넣는 경우를 처리.
    """
    import re

    # 코드블록 내 JSON
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', content)
    if match:
        return match.group(1).strip()

    # 순수 JSON (배열 또는 객체)
    # 첫 번째 [ 또는 { 부터 마지막 ] 또는 } 까지
    content = content.strip()
    if content.startswith(('[', '{')):
        return content

    # 텍스트 사이에 JSON이 있는 경우
    match = re.search(r'(\[[\s\S]*\]|\{[\s\S]*\})', content)
    if match:
        return match.group(1).strip()

    return None


def _extract_block(text: str, pattern: str) -> str:
    """텍스트에서 패턴으로 시작하는 첫 번째 중괄호 블록 추출."""
    import re as _re
    lines = text.split("\n")
    result = []
    in_block = False
    depth = 0

    for line in lines:
        stripped = line.strip()
        if not in_block and _re.search(pattern, stripped):
            in_block = True
            depth = 0
            result = [line]
            depth += stripped.count("{") - stripped.count("}")
            depth += stripped.count("(") - stripped.count(")")
            if depth <= 0 and (stripped.endswith(";") or stripped.endswith("}")):
                return "\n".join(result)
            continue

        if in_block:
            result.append(line)
            depth += stripped.count("{") - stripped.count("}")
            depth += stripped.count("(") - stripped.count(")")
            if depth <= 0 and (stripped.endswith(";") or stripped.endswith("};")):
                return "\n".join(result)

    return "\n".join(result) if result else ""


def _extract_all_blocks(text: str, pattern: str) -> list[str]:
    """텍스트에서 패턴으로 시작하는 모든 중괄호 블록 추출."""
    import re as _re
    lines = text.split("\n")
    blocks = []
    current = []
    in_block = False
    depth = 0

    for line in lines:
        stripped = line.strip()
        if not in_block and _re.search(pattern, stripped):
            in_block = True
            depth = 0
            current = [line]
            depth += stripped.count("{") - stripped.count("}")
            depth += stripped.count("(") - stripped.count(")")
            if depth <= 0 and (stripped.endswith(";") or stripped.endswith("}")):
                blocks.append("\n".join(current))
                in_block = False
            continue

        if in_block:
            current.append(line)
            depth += stripped.count("{") - stripped.count("}")
            depth += stripped.count("(") - stripped.count(")")
            if depth <= 0 and (stripped.endswith(";") or stripped.endswith("};")):
                blocks.append("\n".join(current))
                in_block = False

    return blocks


def _extract_section(text: str, marker: str) -> str:
    """마커 이후의 텍스트 섹션 추출."""
    lines = text.split("\n")
    result = []
    found = False
    for line in lines:
        if marker in line:
            found = True
            continue
        if found:
            if line.strip().startswith("// IMPL:") or line.strip().startswith("// MODAL_CONTENT:"):
                break
            result.append(line)
    return "\n".join(result).strip()
