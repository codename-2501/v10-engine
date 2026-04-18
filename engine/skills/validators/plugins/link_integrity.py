"""HTML 내부 링크 (#anchor) 무결성 검증."""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("link_integrity")
class LinkIntegrityValidator(Validator):
    """HTML 내부 href="#SC-XXX" 링크가 실제 섹션 id 에 매칭되는지 검증.

    외부 링크(http://...) 는 스킵. mailto:, tel: 등도 스킵.
    """
    name = "link_integrity"

    _HREF_RE = re.compile(r'href=["\'](#[^"\']+)["\']')
    _ID_RE = re.compile(r'id=["\']([^"\']+)["\']')

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        if "<" not in content:  # HTML 아닌 경우 스킵
            return PluginValidationResult(passed=True, validator=self.name)

        anchors = {a.lstrip("#") for a in self._HREF_RE.findall(content)}
        ids_defined = set(self._ID_RE.findall(content))

        missing = anchors - ids_defined
        if not missing:
            return PluginValidationResult(passed=True, validator=self.name)

        failures = [
            f"anchor 미정의: #{', #'.join(sorted(missing)[:10])} "
            f"({len(missing)}개)",
        ]
        # 자동 수정: 깨진 #anchor 를 "#" 로 치환 (null link)
        fixes = []
        for a in sorted(missing):
            fixes.append(Fix(
                kind="patch",
                target=rf'href=["\']#{re.escape(a)}["\']',
                replacement='href="#"',
                rationale=f"undefined anchor #{a} → #",
            ))

        return PluginValidationResult(
            passed=False,
            validator=self.name,
            failures=failures,
            fixable_hints=fixes,
        )
