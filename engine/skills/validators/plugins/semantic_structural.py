"""Stage 22-A: Structural-as-Semantic Validator (AI 호출 0).

spec yaml 에 선언된 `screen_type_expectations` 에 따라 각 `<section id="...">`
의 실제 UI 요소 집합이 기대와 맞는지 HTML 파싱 기반으로 검증.

예시:
    screen_type_expectations:
      dashboard:
        required_elements: ["nav", "table|.chart", "[data-metric]|.card"]
        forbidden_elements: []
        match_by: "id_prefix"    # id_prefix | class_contains | aria_role
        key: "SC-AD-"            # dashboard 타입 매칭 규칙
      login:
        required_elements: ["input[type=email]|input[type=text]", "input[type=password]", "button"]
        match_by: "heading_contains"
        key: ["로그인", "login"]

각 required_element 는 "alternative|alternative" 로 OR 표현.
각 forbidden_element 는 하나라도 매칭되면 fail.

매칭 방법 (match_by):
- id_prefix: section.id 가 key 로 시작
- class_contains: section.class 에 key 포함
- heading_contains: section 내 h1~h3 텍스트에 key 중 하나 포함
- explicit: section.data-screen-type == key
"""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("semantic_structural")
class SemanticStructuralValidator(Validator):
    name = "semantic_structural"

    _SECTION_RE = re.compile(
        r'<section([^>]*)>([\s\S]*?)</section>', re.IGNORECASE,
    )

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        expectations = spec.get("screen_type_expectations") or {}
        if not expectations or "<section" not in content:
            return PluginValidationResult(passed=True, validator=self.name)

        failures: list[str] = []
        for attrs, body in self._SECTION_RE.findall(content):
            screen_type = self._match_screen_type(attrs, body, expectations)
            if not screen_type:
                continue

            exp = expectations[screen_type]
            section_id = self._extract_id(attrs) or "(no-id)"

            # required elements
            for selector_spec in exp.get("required_elements", []):
                if not self._any_match(body, selector_spec.split("|")):
                    failures.append(
                        f"{section_id} ({screen_type}): 필수 요소 없음 → "
                        f"{selector_spec}",
                    )
            # forbidden
            for selector_spec in exp.get("forbidden_elements", []):
                if self._any_match(body, selector_spec.split("|")):
                    failures.append(
                        f"{section_id} ({screen_type}): 금지 요소 존재 → "
                        f"{selector_spec}",
                    )

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures[:20],
        )

    @staticmethod
    def _extract_id(attrs: str) -> str:
        m = re.search(r'id=["\']([^"\']+)["\']', attrs)
        return m.group(1) if m else ""

    @staticmethod
    def _match_screen_type(attrs: str, body: str, expectations: dict) -> str | None:
        for screen_type, rules in expectations.items():
            match_by = rules.get("match_by", "id_prefix")
            key = rules.get("key", "")
            if not key:
                continue
            keys = [key] if isinstance(key, str) else list(key)

            if match_by == "id_prefix":
                sid = SemanticStructuralValidator._extract_id(attrs)
                if any(sid.startswith(k) for k in keys):
                    return screen_type
            elif match_by == "class_contains":
                m = re.search(r'class=["\']([^"\']+)["\']', attrs)
                if m and any(k in m.group(1) for k in keys):
                    return screen_type
            elif match_by == "heading_contains":
                hs = re.findall(r"<h[1-3][^>]*>([\s\S]*?)</h[1-3]>", body)
                text = " ".join(hs).lower()
                if any(k.lower() in text for k in keys):
                    return screen_type
            elif match_by == "explicit":
                m = re.search(r'data-screen-type=["\']([^"\']+)["\']', attrs)
                if m and m.group(1) in keys:
                    return screen_type
        return None

    @staticmethod
    def _any_match(body: str, selectors: list[str]) -> bool:
        """각 selector (간단 CSS-like) 가 body 에 매칭되는지."""
        for sel in selectors:
            sel = sel.strip()
            if not sel:
                continue
            if SemanticStructuralValidator._match_one(body, sel):
                return True
        return False

    @staticmethod
    def _match_one(body: str, sel: str) -> bool:
        """간단한 selector 매칭 (tag/.class/[attr]/tag[attr=val])."""
        # 예: nav | table | .chart | [data-metric] | input[type=email] | button
        # 태그 + 속성
        m = re.match(r"^([a-z][a-z0-9]*)?(\[.*?\])?(\.[\w-]+)?$", sel)
        if not m:
            return False
        tag, attr_pat, class_pat = m.group(1), m.group(2), m.group(3)

        if not tag and class_pat:
            cls = class_pat.lstrip(".")
            return bool(re.search(rf'class=["\'][^"\']*\b{re.escape(cls)}\b', body))

        if attr_pat:
            # [attr] 또는 [attr=val]
            inside = attr_pat.strip("[]")
            if "=" in inside:
                attr_name, val = inside.split("=", 1)
                val = val.strip("\"' ")
                pattern = rf'<{tag or "[a-z][a-z0-9]*"}[^>]*{attr_name}=["\']{re.escape(val)}["\']'
            else:
                pattern = rf'<{tag or "[a-z][a-z0-9]*"}[^>]*\b{inside}\b'
            return bool(re.search(pattern, body, re.IGNORECASE))

        if tag:
            return bool(re.search(rf"<{tag}[\s>]", body, re.IGNORECASE))

        return False
