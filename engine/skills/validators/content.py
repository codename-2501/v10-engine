"""
Content validator — checks artifact content quality against spec rules.

Inspects for forbidden patterns, length constraints, and required elements.
"""

from __future__ import annotations

import re
from typing import Dict, List


# Default forbidden patterns — common placeholder text that should never
# appear in a production artifact.
_DEFAULT_FORBIDDEN = [
    r"\bTODO\b",
    r"\bTBD\b",
    r"\b미정\b",
]


class ContentValidator:
    """Static methods for content-quality validation of artifacts."""

    @staticmethod
    def validate(content: str, rules: Dict) -> List[str]:
        """Run all content checks defined in *rules*.

        Args:
            content: The artifact text to inspect.
            rules:   A dict that may contain ``forbidden_patterns``,
                     ``min_chars``, ``max_chars``, and ``has_code_blocks``
                     keys.

        Returns:
            A list of human-readable issue strings (empty if all pass).
        """
        issues: List[str] = []

        # Forbidden patterns (use defaults if not specified)
        forbidden = rules.get("forbidden_patterns", _DEFAULT_FORBIDDEN)
        if forbidden:
            issues.extend(ContentValidator._check_forbidden(content, forbidden))

        # Minimum character count
        min_chars = rules.get("min_chars")
        if min_chars is not None:
            issues.extend(
                ContentValidator._check_min_chars(content, int(min_chars))
            )

        # Maximum character count
        max_chars = rules.get("max_chars")
        if max_chars is not None:
            issues.extend(
                ContentValidator._check_max_chars(content, int(max_chars))
            )

        # Code blocks requirement
        has_code_blocks = rules.get("has_code_blocks")
        if has_code_blocks:
            issues.extend(ContentValidator._check_code_blocks(content))

        return issues

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_forbidden(content: str, patterns: List[str]) -> List[str]:
        """Scan for forbidden patterns.

        Args:
            content:  Artifact text.
            patterns: List of regex patterns that must NOT appear.

        Returns:
            List of issues for each pattern found.
        """
        # 자가검증 블록 제거 — AI가 "금지 키워드(TODO/TBD/미정) 없음" 같은
        # 체크리스트를 산출물 끝에 붙이면 금지 패턴 오탐이 발생함
        cleaned = content
        # <!-- SELF_CHECK ... --> HTML 주석 제거
        cleaned = re.sub(r'<!--\s*SELF_CHECK.*?-->', '', cleaned, flags=re.DOTALL)
        # 자체 검증 체크리스트 섹션 제거 (## 자체 검증 ~ 끝까지)
        cleaned = re.sub(r'##\s*자체\s*검증.*$', '', cleaned, flags=re.DOTALL)
        # SELF_CHECK JSON 블록 제거
        cleaned = re.sub(r'SELF_CHECK\s*:\s*\{[^}]*\}', '', cleaned)
        # "금지 키워드 ... 없음" 패턴이 포함된 줄 제거 (체크리스트/테이블)
        cleaned = re.sub(r'^.*금지\s*키워드.*없음.*$', '', cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r'^.*forbidden.*keyword.*none.*$', '', cleaned, flags=re.MULTILINE | re.IGNORECASE)

        issues: List[str] = []
        for pattern in patterns:
            matches = re.findall(pattern, cleaned, re.IGNORECASE)
            if matches:
                issues.append(
                    f"금지 패턴 발견: '{pattern}' ({len(matches)}건)"
                )
        return issues

    @staticmethod
    def _check_min_chars(content: str, min_chars: int) -> List[str]:
        """Verify minimum character count.

        Args:
            content:   Artifact text.
            min_chars: Minimum number of characters required.

        Returns:
            List containing one issue if the content is too short.
        """
        char_count = len(content.strip())
        if char_count < min_chars:
            return [
                f"최소 분량 미달: {char_count}자 작성, "
                f"최소 {min_chars}자 필요"
            ]
        return []

    @staticmethod
    def _check_max_chars(content: str, max_chars: int) -> List[str]:
        """Verify maximum character count.

        Args:
            content:   Artifact text.
            max_chars: Maximum number of characters allowed.

        Returns:
            List containing one issue if the content is too long.
        """
        char_count = len(content.strip())
        if char_count > max_chars:
            return [
                f"최대 분량 초과: {char_count}자 작성, "
                f"최대 {max_chars}자 이하여야 함"
            ]
        return []

    @staticmethod
    def _check_code_blocks(content: str) -> List[str]:
        """Verify that at least one fenced code block exists.

        Args:
            content: Artifact text.

        Returns:
            List containing one issue if no code block is found.
        """
        if not re.search(r"```", content):
            return ["필수 코드 블록 누락: ``` 블록이 필요합니다"]
        return []
