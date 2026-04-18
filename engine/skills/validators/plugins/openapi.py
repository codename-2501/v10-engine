"""API 설계서 path ↔ schema 참조 무결성."""
from __future__ import annotations

import json
import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("openapi")
class OpenAPIValidator(Validator):
    """OpenAPI 스타일 API 설계서 검증.

    대상:
      1) YAML/JSON OpenAPI 블록 (코드 펜스 안)
      2) markdown API 명세 — HTTP method + path + request/response schema 테이블
    """
    name = "openapi"

    _HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")
    _PATH_RE = re.compile(r"`?(/[A-Za-z0-9_\-/{}:.]+)`?")

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        failures: list[str] = []

        # 1) JSON/YAML OpenAPI 블록 분석
        json_blocks = re.findall(
            r"```(?:json|yaml)\s+([\s\S]*?)```", content, flags=re.I,
        )
        for block in json_blocks:
            block = block.strip()
            if not block.startswith("{") and "openapi:" not in block.lower():
                continue
            # JSON 파싱 시도
            try:
                obj = json.loads(block) if block.startswith("{") else None
            except Exception:
                obj = None
            if obj and "paths" in obj:
                failures.extend(self._check_openapi_obj(obj))

        # 2) 프리폼 API 명세 — HTTP method + path 패턴
        method_path_pattern = re.compile(
            r"\b(" + "|".join(self._HTTP_METHODS) + r")\s+(/[^\s<]+)",
        )
        seen_endpoints: set[tuple[str, str]] = set()
        for m in method_path_pattern.findall(content):
            method, path = m[0], m[1].strip("`")
            # path parameter 형식 검증 — {id} / :id 중 일관성
            if "{" in path and ":" in path:
                failures.append(
                    f"{method} {path}: path 파라미터 스타일 혼용 (중괄호+콜론)",
                )
            key = (method.upper(), path)
            if key in seen_endpoints:
                failures.append(f"중복 엔드포인트: {method} {path}")
            seen_endpoints.add(key)

        # 3) schema $ref 정의 존재 여부 (OpenAPI 블록에서)
        schemas_defined: set[str] = set()
        refs_used: set[str] = set()
        for m in re.finditer(r"\$ref:\s*['\"]?#/components/schemas/(\w+)", content):
            refs_used.add(m.group(1))
        for m in re.finditer(r"^\s*(\w+):\s*(?:\n\s+type:|\n\s+properties:)",
                             content, flags=re.M):
            schemas_defined.add(m.group(1))
        missing_refs = refs_used - schemas_defined
        if missing_refs:
            failures.append(
                f"미정의 schema 참조: {', '.join(sorted(missing_refs)[:10])}",
            )

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures[:20],
        )

    def _check_openapi_obj(self, obj: dict) -> list[str]:
        fails = []
        paths = obj.get("paths") or {}
        components = (obj.get("components") or {}).get("schemas") or {}
        schemas_defined = set(components.keys())
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.upper() not in self._HTTP_METHODS:
                    continue
                # request/response schema $ref 확인
                refs = self._collect_refs(op)
                missing = refs - schemas_defined
                for r in missing:
                    fails.append(
                        f"{method.upper()} {path}: $ref '{r}' 미정의",
                    )
        return fails

    @staticmethod
    def _collect_refs(obj) -> set[str]:
        out: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "$ref" and isinstance(v, str):
                    m = re.search(r"#/components/schemas/(\w+)", v)
                    if m:
                        out.add(m.group(1))
                else:
                    out |= OpenAPIValidator._collect_refs(v)
        elif isinstance(obj, list):
            for item in obj:
                out |= OpenAPIValidator._collect_refs(item)
        return out
