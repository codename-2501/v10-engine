"""CSS 클래스 사용(class=) vs 정의(<style> 내 .cls{}) 대조."""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("css_class_integrity")
class CSSClassIntegrityValidator(Validator):
    """HTML 에서 사용한 class 가 어떤 <style> 블록에도 정의되지 않은 비율 검증.

    Stage 2-D 패치 이후 예방적 방어. 임계 (기본 50%) 초과 시 failure.
    자동 수정 불가 (플러그인 범위 밖) — 경고만.
    """
    name = "css_class_integrity"

    _CLASS_RE = re.compile(r'class=["\']([^"\']+)["\']')
    _STYLE_RE = re.compile(r"<style[^>]*>([\s\S]*?)</style>")
    _CLASS_DEF_RE = re.compile(r"\.([a-zA-Z][\w-]*)\s*[,{]")

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        if "<style" not in content or "class=" not in content:
            return PluginValidationResult(passed=True, validator=self.name)

        cfg = (spec.get("validator_config") or {}).get("css_class_integrity") or {}
        max_missing_ratio = float(cfg.get("max_missing_ratio", 0.5))

        classes_used: set[str] = set()
        for m in self._CLASS_RE.findall(content):
            classes_used.update(m.split())

        classes_defined: set[str] = set()
        for style_block in self._STYLE_RE.findall(content):
            classes_defined.update(self._CLASS_DEF_RE.findall(style_block))

        if not classes_used:
            return PluginValidationResult(passed=True, validator=self.name)

        missing = classes_used - classes_defined
        ratio = len(missing) / len(classes_used)

        if ratio > max_missing_ratio:
            return PluginValidationResult(
                passed=False,
                validator=self.name,
                failures=[
                    f"클래스 정의 유실 {len(missing)}/{len(classes_used)} "
                    f"({ratio:.0%} > 임계 {max_missing_ratio:.0%})",
                    f"미정의 예: {', '.join(sorted(missing)[:8])}",
                ],
                fixable_hints=[],  # 자동 수정 범위 밖
            )
        return PluginValidationResult(passed=True, validator=self.name)
