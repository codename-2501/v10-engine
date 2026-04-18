"""디자인 토큰 var(--...) 참조 검증."""
from __future__ import annotations

import json
import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("token_reference")
class TokenReferenceValidator(Validator):
    """산출물에서 쓰는 `var(--name)` 이 디자인 토큰 artifact 에 실제로 정의됐는지.

    context.design_tokens (dict) 가 있으면 사용. 없으면 HTML 내부 :root 의 정의 참조.
    """
    name = "token_reference"

    _VAR_USE_RE = re.compile(r"var\(--([a-z][a-z0-9_-]*)\)")
    _VAR_DEF_RE = re.compile(r"--([a-z][a-z0-9_-]*)\s*:")

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        if "var(--" not in content:
            return PluginValidationResult(passed=True, validator=self.name)

        used = set(self._VAR_USE_RE.findall(content))
        defined: set[str] = set()

        # 1) context.design_tokens (외부 주입)
        tokens = (context or {}).get("design_tokens")
        if isinstance(tokens, dict):
            defined.update(self._flatten_token_names(tokens))

        # 2) content 내 :root { --... : ... } 정의
        defined.update(self._VAR_DEF_RE.findall(content))

        missing = used - defined
        if not missing:
            return PluginValidationResult(passed=True, validator=self.name)

        return PluginValidationResult(
            passed=False,
            validator=self.name,
            failures=[
                f"미정의 CSS 변수 사용: "
                f"{', '.join('--' + m for m in sorted(missing)[:10])}",
            ],
            fixable_hints=[],  # 자동 수정 없음 — 경고만
        )

    @staticmethod
    def _flatten_token_names(d: dict, prefix: str = "") -> list[str]:
        names = []
        for k, v in d.items():
            key = f"{prefix}-{k}" if prefix else k
            if isinstance(v, dict):
                names.extend(TokenReferenceValidator._flatten_token_names(v, key))
            else:
                names.append(key)
        return names
