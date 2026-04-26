"""Pillar 1 — Dual Output Dispatcher.

LLM 한 번 호출에서 두 형식 출력 ("---FORMAT:xxx---" 마커로 분리).
각 형식별 schema 검증 + role 별 분리.

사용 예 (spec yaml):
  outputs:
    - format: markdown
      file_role: human_review
    - format: openapi_yaml
      file_role: developer_consumable
      schema_ref: schemas/openapi-3.1.json
      strict: true

LLM 응답 예:
  ---FORMAT:markdown---
  # API 설계서
  ...
  ---FORMAT:openapi_yaml---
  openapi: 3.1.0
  ...
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FORMATS_CACHE: dict | None = None
_FORMATS_PATH = (
    Path(__file__).parent.parent / "specs" / "_common" / "output_formats.yaml"
)
# ---FORMAT:xxx--- (앞뒤 공백 허용, 한 줄만)
_MARKER_RE = re.compile(
    r"^[ \t]*---[ \t]*FORMAT[ \t]*:[ \t]*([a-z][a-z0-9_]*)[ \t]*---[ \t]*$",
    re.MULTILINE,
)


@dataclass
class DualOutputPart:
    format: str
    file_role: str = "human_review"
    schema_ref: str | None = None
    strict: bool = False
    on_fail: str = "warn"
    content: str = ""
    validation_pass: bool = True
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class DualOutputResult:
    parts: list[DualOutputPart] = field(default_factory=list)
    has_marker: bool = False
    fallback_used: bool = False  # 마커 누락 → single 출력 처리

    def by_role(self, role: str) -> DualOutputPart | None:
        for p in self.parts:
            if p.file_role == role:
                return p
        return None


def load_output_formats() -> dict:
    """_common/output_formats.yaml 로드 (캐시)."""
    global _FORMATS_CACHE
    if _FORMATS_CACHE is not None:
        return _FORMATS_CACHE
    if not _FORMATS_PATH.exists():
        _FORMATS_CACHE = {}
        return _FORMATS_CACHE
    try:
        with _FORMATS_PATH.open(encoding="utf-8") as f:
            _FORMATS_CACHE = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("output_formats_load_fail %s", e)
        _FORMATS_CACHE = {}
    return _FORMATS_CACHE


def split_by_marker(content: str) -> list[tuple[str, str]]:
    """LLM 응답을 ---FORMAT:xxx--- 마커로 분리.

    Returns: [(format_name, content), ...]
    마커 없으면 빈 list — 호출자가 fallback 처리.
    """
    matches = list(_MARKER_RE.finditer(content))
    if not matches:
        return []
    parts: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        fmt = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        parts.append((fmt, body))
    return parts


def is_dual_output_spec(spec: dict) -> bool:
    """spec.outputs 가 list 면 dual output."""
    outs = spec.get("outputs")
    return isinstance(outs, list) and len(outs) >= 2


def dual_output_directive() -> str:
    """spec prompt 끝에 자동 주입할 instruction.

    LLM 이 마커로 두 형식을 명확히 분리하도록 강제.
    """
    return (
        "\n\n## 출력 형식 (둘 다 출력 — 마커로 명확히 구분)\n"
        "각 형식 앞에 정확히 다음 마커 라인을 출력하세요 (앞뒤 공백 없이):\n"
        "  ---FORMAT:<format_name>---\n"
        "그리고 그 다음 줄부터 해당 형식의 콘텐츠.\n"
        "코드 블록(```) 으로 감싸지 마세요.\n"
        "예:\n"
        "---FORMAT:markdown---\n"
        "# 인간 검토용 ...\n"
        "(중략)\n"
        "---FORMAT:openapi_yaml---\n"
        "openapi: 3.1.0\n"
        "info:\n"
        "  ...\n"
    )


async def parse_and_validate_dual(
    spec: dict, raw_content: str,
) -> DualOutputResult:
    """LLM 응답을 spec.outputs 정의에 맞게 분리 + 각 형식 schema 검증.

    마커 누락 시 fallback — single 출력으로 처리 (첫 outputs 항목에 그대로 할당).
    """
    outputs_cfg = spec.get("outputs", [])
    parts_split = split_by_marker(raw_content)
    result = DualOutputResult(has_marker=bool(parts_split))

    if not parts_split:
        # fallback — 마커 누락. 첫 outputs 항목에 그대로
        result.fallback_used = True
        if outputs_cfg:
            first = outputs_cfg[0]
            result.parts.append(DualOutputPart(
                format=first.get("format", "document"),
                file_role=first.get("file_role", "human_review"),
                schema_ref=first.get("schema_ref"),
                strict=bool(first.get("strict", False)),
                on_fail=first.get("on_fail", "warn"),
                content=raw_content,
            ))
        logger.warning(
            "dual_output_marker_missing spec=%s — single fallback",
            spec.get("name", "?"),
        )
        return result

    # 마커 매칭 — 각 형식별 cfg 찾기
    cfg_by_format = {o.get("format"): o for o in outputs_cfg}
    for fmt, body in parts_split:
        cfg = cfg_by_format.get(fmt, {})
        part = DualOutputPart(
            format=fmt,
            file_role=cfg.get("file_role", "human_review"),
            schema_ref=cfg.get("schema_ref"),
            strict=bool(cfg.get("strict", False)),
            on_fail=cfg.get("on_fail", "warn"),
            content=body,
        )
        # schema 검증 (strict 일 때만)
        if part.strict and part.schema_ref:
            try:
                from engine.skills.qa.schema_validator import (
                    validate_against_schema,
                )
                vr = validate_against_schema(body, part.schema_ref)
                part.validation_pass = vr.pass_
                part.validation_errors = vr.errors
                if not vr.pass_:
                    logger.warning(
                        "dual_output_schema_fail spec=%s format=%s errors=%d",
                        spec.get("name", "?"), fmt, len(vr.errors),
                    )
            except Exception as e:
                logger.warning(
                    "dual_output_validate_fail spec=%s format=%s err=%s",
                    spec.get("name", "?"), fmt, e,
                )
        result.parts.append(part)
    return result
