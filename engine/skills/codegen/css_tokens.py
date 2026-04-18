"""CSS 토큰 변환 모듈 — 디자인 토큰을 CSS 변수로 변환.

executor.py에서 분리된 순수 함수 모듈.
"""

from __future__ import annotations

from typing import Any


def tokens_to_css_vars(tokens: dict[str, Any]) -> dict[str, str]:
    """디자인 토큰 객체를 CSS 변수명으로 변환.

    예: {"color": {"primary": "#007ACC"}} → {"--color-primary": "#007ACC"}
    """
    result = {}

    def flatten(obj: dict[str, Any], prefix: str = "") -> None:
        for key, value in obj.items():
            var_name = f"--{prefix}{key}".replace(" ", "-").lower()

            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}-")
            elif isinstance(value, (str, int, float)):
                result[var_name] = str(value)

    flatten(tokens)
    return result


def build_style_from_design_tokens(tokens: dict[str, Any]) -> str:
    """디자인 토큰으로부터 :root CSS 스타일 블록 생성."""
    css_vars = tokens_to_css_vars(tokens)

    if not css_vars:
        return ""

    lines = [":root {"]
    for var_name, value in sorted(css_vars.items()):
        lines.append(f"  {var_name}: {value};")
    lines.append("}")

    return "\n".join(lines)
