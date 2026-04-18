"""
Structural validator — checks document structure against spec rules.

Inspects headings, tables, and section counts in Markdown content.
"""

from __future__ import annotations

import re
from typing import Dict, List


class StructureValidator:
    """Static methods for structural validation of Markdown artifacts."""

    @staticmethod
    def validate(content: str, rules: Dict) -> List[str]:
        """Run all structural checks defined in *rules*.

        Args:
            content: The Markdown text to inspect.
            rules:   A dict that may contain ``required_headings``,
                     ``required_tables``, and ``min_sections`` keys.

        Returns:
            A list of human-readable issue strings (empty if all pass).
        """
        issues: List[str] = []

        required_headings = rules.get("required_headings", [])
        if required_headings:
            issues.extend(
                StructureValidator._check_headings(content, required_headings)
            )

        required_tables = rules.get("required_tables")
        if required_tables is not None:
            issues.extend(
                StructureValidator._check_tables(content, int(required_tables))
            )

        min_sections = rules.get("min_sections")
        if min_sections is not None:
            issues.extend(
                StructureValidator._check_sections(content, int(min_sections))
            )

        return issues

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_headings(content: str, required: List[str]) -> List[str]:
        """Verify that every required heading exists (fuzzy regex match).

        Each required heading is treated as a regex pattern and matched
        against lines that start with one or more ``#`` characters.

        Args:
            content:  Markdown text.
            required: List of heading patterns (plain text or regex).

        Returns:
            List of issues for missing headings.
        """
        issues: List[str] = []
        for heading in required:
            # 핵심 텍스트 추출 (괄호 내용 제거)
            core = re.sub(r"\s*\(.*?\)\s*", "", heading).strip()
            # 1차: 핵심 텍스트로 유연 매칭
            pattern = r"^#{1,6}\s+.*" + re.escape(core)
            if not re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
                # 2차: 원본 전체로 정확 매칭
                pattern_full = r"^#{1,6}\s+.*" + re.escape(heading)
                if not re.search(pattern_full, content, re.MULTILINE | re.IGNORECASE):
                    # 3차: 공백/특수문자 무시 매칭
                    normalized = re.sub(r"[^\w가-힣]", "", heading)
                    content_normalized = re.sub(r"[^\w가-힣\n#]", "", content)
                    if not re.search(r"^#{1,6}\s*.*" + normalized, content_normalized, re.MULTILINE | re.IGNORECASE):
                        issues.append(f"필수 섹션 누락: '{heading}'")
        return issues

    @staticmethod
    def _check_tables(content: str, min_count: int) -> List[str]:
        """Count Markdown tables by looking for separator rows (``|---|``).

        Args:
            content:   Markdown text.
            min_count: Minimum number of tables expected.

        Returns:
            List containing one issue if the count is below the minimum.
        """
        # A table separator row contains at least one "|---|" segment.
        table_separators = re.findall(
            r"^\|[\s\-:|]+\|",
            content,
            re.MULTILINE,
        )
        if len(table_separators) < min_count:
            return [
                f"테이블 부족: {len(table_separators)}개 발견, "
                f"최소 {min_count}개 필요"
            ]
        return []

    @staticmethod
    def _check_sections(content: str, min_count: int) -> List[str]:
        """Count ``##`` level headings.

        Args:
            content:   Markdown text.
            min_count: Minimum number of ``##`` sections expected.

        Returns:
            List containing one issue if the count is below the minimum.
        """
        sections = re.findall(r"^##\s+", content, re.MULTILINE)
        if len(sections) < min_count:
            return [
                f"섹션 부족: {len(sections)}개 발견, "
                f"최소 {min_count}개 필요"
            ]
        return []
