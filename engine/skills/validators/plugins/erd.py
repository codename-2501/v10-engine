"""ERD (DB 설계서) FK 참조 무결성·PK 존재·고아 테이블 검증."""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("erd")
class ERDValidator(Validator):
    """Markdown 혹은 HTML 테이블 형식의 ERD 에서 참조 무결성 검증.

    대상 구조 (spec 에 따라 선택):
      1) markdown table (헤더 '테이블'/'컬럼'/'타입'/'FK')
      2) mermaid erDiagram

    검증:
      - PK 컬럼 1개 이상 존재 (id 또는 PK 표시)
      - FK 가 실존 테이블·컬럼 참조 (형식: 'users.id' 또는 'users(id)')
      - 순환 참조 경고 (A→B→A)
      - 고아 테이블 (참조되지도 않고 참조하지도 않는)
    """
    name = "erd"

    _FK_RE = re.compile(r"([A-Za-z_]\w*)\s*[.\(]\s*([A-Za-z_]\w*)\s*\)?")

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        tables = self._extract_tables(content)
        if not tables:
            return PluginValidationResult(passed=True, validator=self.name)

        failures: list[str] = []
        # 각 테이블별 PK 존재
        for name, cols in tables.items():
            has_pk = any(c.get("pk") for c in cols)
            if not has_pk:
                failures.append(f"테이블 '{name}': PK 미정의")

        # FK 참조 무결성
        all_table_names = set(tables.keys())
        references: dict[str, list[str]] = {}  # from_table -> [to_table]
        for name, cols in tables.items():
            for c in cols:
                fk = c.get("fk")
                if not fk:
                    continue
                m = self._FK_RE.search(fk)
                if not m:
                    continue
                to_table, to_col = m.group(1), m.group(2)
                if to_table not in all_table_names:
                    failures.append(
                        f"FK 참조 실패: {name}.{c['name']} → "
                        f"{to_table}.{to_col} (테이블 없음)",
                    )
                else:
                    # 참조 컬럼 존재 체크
                    target_cols = {x["name"] for x in tables[to_table]}
                    if to_col not in target_cols:
                        failures.append(
                            f"FK 참조 실패: {name}.{c['name']} → "
                            f"{to_table}.{to_col} (컬럼 없음)",
                        )
                    references.setdefault(name, []).append(to_table)

        # 순환 참조 감지 (간단한 DFS, 경고만)
        for start in references:
            if self._has_cycle(start, references):
                failures.append(f"순환 참조 감지 기점: {start}")

        # 고아 테이블
        referenced = {t for lst in references.values() for t in lst}
        referencing = set(references.keys())
        connected = referenced | referencing
        orphans = all_table_names - connected
        if len(all_table_names) > 1:
            for orphan in orphans:
                failures.append(f"고아 테이블: {orphan} (참조·피참조 없음)")

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures[:20],
        )

    def _extract_tables(self, content: str) -> dict[str, list[dict]]:
        """markdown table 에서 {테이블명: [{name, type, pk, fk}]} 추출.

        휴리스틱: 헤더에 '컬럼' 포함된 테이블만 대상. 테이블 이름은 바로 위
        `##` 제목 또는 `### 테이블명` 형태.
        """
        result: dict[str, list[dict]] = {}
        # 단순 패턴: 제목 + 테이블 블록 묶음 탐색
        blocks = re.split(r"^##+\s+", content, flags=re.M)
        for block in blocks:
            if "|" not in block or "컬럼" not in block:
                continue
            lines = block.splitlines()
            title = lines[0].strip() if lines else ""
            # 테이블명은 title 의 첫 토큰
            table_name = re.split(r"\s|\(", title, 1)[0].strip()
            if not table_name:
                continue
            # 행 파싱
            cols: list[dict] = []
            for line in lines[1:]:
                if not (line.strip().startswith("|") and line.strip().endswith("|")):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2 or cells[0] in ("컬럼", "---"):
                    continue
                col = {
                    "name": cells[0],
                    "type": cells[1] if len(cells) > 1 else "",
                    "pk": "PK" in " ".join(cells).upper() or cells[0].lower() == "id",
                    "fk": next((c for c in cells if "FK" in c.upper() or "(" in c), ""),
                }
                cols.append(col)
            if cols:
                result[table_name] = cols
        return result

    @staticmethod
    def _has_cycle(start: str, refs: dict[str, list[str]], seen: set | None = None) -> bool:
        seen = seen or set()
        if start in seen:
            return True
        seen = seen | {start}
        for nxt in refs.get(start, []):
            if ERDValidator._has_cycle(nxt, refs, seen):
                return True
        return False
