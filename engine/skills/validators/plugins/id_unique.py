"""ID 중복·패턴·상위문서 매칭 검증 plugin."""
from __future__ import annotations

import re
from typing import Any

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)

DEFAULT_PATTERNS = [
    r"SC-[A-Z]{2,4}-\d{3,4}",
    r"SCR-\d{3,4}",
    r"[A-Z]{2,5}-[A-Z]{2,4}-\d{3,4}",
]


@register("id_unique")
class IDUniqueValidator(Validator):
    """
    spec yaml 설정:
        validators: [id_unique]
        validator_config:
          id_unique:
            pattern: "SC-[A-Z]{2,4}-\\d{3,4}"      # 선택, 기본 패턴 사용 가능
            source_ids: []                           # 상위 문서 매칭 대상 (선택)
    """
    name = "id_unique"

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        cfg = (spec.get("validator_config") or {}).get("id_unique") or {}
        pattern = cfg.get("pattern")
        patterns: list[str] = [pattern] if pattern else DEFAULT_PATTERNS
        source_ids: list[str] = cfg.get("source_ids") or []

        all_found: list[str] = []
        for pat in patterns:
            try:
                all_found.extend(re.findall(pat, content))
            except re.error:
                continue

        unique = set(all_found)
        dupes = [i for i in unique if all_found.count(i) > 1]

        failures: list[str] = []
        fixes: list[Fix] = []

        if dupes:
            failures.append(
                f"중복 ID 감지: {', '.join(sorted(dupes)[:10])} "
                f"(총 {len(dupes)}종)",
            )
            # 자동 수정 비활성화 — ID rename 은 ID 오염 야기
            # (nav 링크의 타 페이지 ID 참조를 중복으로 오판해 섹션 ID 까지 변형)
            # 감지만 하고 수정은 하지 않음 (경고 로그로 충분)

        if source_ids:
            missing = set(source_ids) - unique
            if missing:
                failures.append(
                    f"상위 문서의 ID 누락: {', '.join(sorted(missing)[:10])} "
                    f"(총 {len(missing)}종)",
                )
            extra = unique - set(source_ids)
            if extra:
                failures.append(
                    f"상위 문서에 없는 ID 등장: "
                    f"{', '.join(sorted(extra)[:10])}",
                )

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures,
            fixable_hints=fixes,
        )
