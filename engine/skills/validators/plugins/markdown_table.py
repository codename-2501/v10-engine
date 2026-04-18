"""Markdown 테이블 형식·내용 검증 plugin."""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("markdown_table")
class MarkdownTableValidator(Validator):
    """
    Markdown table 각 행의 열 수 일치, 헤더 필수 존재, 빈 셀 감지.

    spec yaml:
        validators: [markdown_table]
        validator_config:
          markdown_table:
            required_headers: ["ID", "이름", "설명"]   # 선택
            min_rows: 3                                 # 선택
    """
    name = "markdown_table"

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        cfg = (spec.get("validator_config") or {}).get("markdown_table") or {}
        required_headers: list[str] = cfg.get("required_headers") or []
        min_rows = int(cfg.get("min_rows") or 0)

        failures: list[str] = []
        fixes: list[Fix] = []

        # 모든 markdown table block 찾기 (| ... | 형식)
        tables = self._find_tables(content)
        if not tables:
            return PluginValidationResult(
                passed=True, validator=self.name,
            )

        for idx, tbl in enumerate(tables):
            lines = tbl.splitlines()
            if len(lines) < 2:
                continue
            header_cells = self._split_row(lines[0])
            header_col_count = len(header_cells)

            # separator 행 (| --- | --- |) 은 2번째 행
            data_rows = lines[2:] if len(lines) >= 3 else []

            # 1) 헤더 체크
            if required_headers:
                missing_headers = [
                    h for h in required_headers if h not in header_cells
                ]
                if missing_headers:
                    failures.append(
                        f"테이블 #{idx+1}: 필수 헤더 누락 {missing_headers}",
                    )

            # 2) 행 개수
            if min_rows and len(data_rows) < min_rows:
                failures.append(
                    f"테이블 #{idx+1}: 행 수 {len(data_rows)} < 최소 {min_rows}",
                )

            # 3) 열 수 일치 + 빈 셀
            for ri, row in enumerate(data_rows):
                cells = self._split_row(row)
                if len(cells) != header_col_count:
                    failures.append(
                        f"테이블 #{idx+1} 행 #{ri+1}: 열 수 {len(cells)} ≠ 헤더 {header_col_count}",
                    )
                empty_cells = [i for i, c in enumerate(cells) if not c.strip()]
                if empty_cells:
                    failures.append(
                        f"테이블 #{idx+1} 행 #{ri+1}: 빈 셀 {empty_cells}",
                    )
                    # 자동 수정: 빈 셀 "-" 채움 (첫 빈 셀만 예시 fix)
                    broken_row_pattern = re.escape(row)
                    fixed_row = row.replace("| |", "| - |").replace(
                        "||", "| - |",
                    )
                    if fixed_row != row:
                        fixes.append(Fix(
                            kind="patch",
                            target=broken_row_pattern,
                            replacement=fixed_row,
                            rationale="빈 셀을 '-' 로 채움",
                        ))

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures,
            fixable_hints=fixes,
        )

    @staticmethod
    def _find_tables(content: str) -> list[str]:
        """인접 `| ... |` 행 묶음 추출."""
        tables = []
        cur: list[str] = []
        for line in content.splitlines():
            if line.strip().startswith("|") and line.strip().endswith("|"):
                cur.append(line)
            else:
                if len(cur) >= 2:
                    tables.append("\n".join(cur))
                cur = []
        if len(cur) >= 2:
            tables.append("\n".join(cur))
        return tables

    @staticmethod
    def _split_row(row: str) -> list[str]:
        parts = [c.strip() for c in row.strip().strip("|").split("|")]
        return parts
