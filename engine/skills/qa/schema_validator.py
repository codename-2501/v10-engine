"""Pillar 5 — output_schema strict + retry_with_feedback.

spec yaml 의 output_schema 가 strict=true 일 때 LLM 응답을 schema 로 검증.
실패 시 errors 를 prompt 에 피드백해 LLM 재호출 (max_retries 만큼).
여전히 실패면 ValidationFail 발생 → 호출자가 NEEDS_HUMAN 처리.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).parent.parent / "specs" / "schemas"
_schema_cache: dict[str, dict] = {}


@dataclass
class ValidationResult:
    pass_: bool = True
    errors: list[str] = field(default_factory=list)
    schema_ref: str = ""


def load_schema(schema_ref: str) -> dict | None:
    """schemas/ 디렉터리에서 JSON Schema 로드 (캐시 사용)."""
    if schema_ref in _schema_cache:
        return _schema_cache[schema_ref]
    # schema_ref 가 "schemas/foo.json" 형식이면 prefix 제거
    rel = schema_ref.removeprefix("schemas/")
    path = _SCHEMAS_DIR / rel
    if not path.exists():
        logger.warning("schema_not_found ref=%s path=%s", schema_ref, path)
        return None
    try:
        with path.open(encoding="utf-8") as f:
            schema = json.load(f)
        _schema_cache[schema_ref] = schema
        return schema
    except Exception as e:
        logger.warning("schema_load_fail ref=%s err=%s", schema_ref, e)
        return None


def validate_against_schema(
    content: str | dict, schema_ref: str
) -> ValidationResult:
    """LLM 응답 (str JSON 또는 dict) 을 schema 로 검증."""
    schema = load_schema(schema_ref)
    if schema is None:
        # schema 부재 → pass (graceful degrade, log only)
        return ValidationResult(pass_=True, schema_ref=schema_ref)

    # str → dict 파싱
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            return ValidationResult(
                pass_=False,
                errors=[f"JSON 파싱 실패: {e}"],
                schema_ref=schema_ref,
            )
    else:
        data = content

    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for err in validator.iter_errors(data):
        path = "/".join(str(p) for p in err.absolute_path)
        errors.append(f"[{path or 'root'}] {err.message}")
        if len(errors) >= 10:
            errors.append("(... 추가 오류 생략)")
            break
    return ValidationResult(
        pass_=len(errors) == 0,
        errors=errors,
        schema_ref=schema_ref,
    )


def build_retry_prompt(
    original_prompt: str, errors: list[str], schema_ref: str
) -> str:
    """schema 검증 실패 시 retry prompt — error 명시 + 원래 spec 재사용."""
    err_block = "\n".join(f"  - {e}" for e in errors[:10])
    return (
        f"## 이전 출력이 schema 검증 실패\n"
        f"schema: {schema_ref}\n"
        f"errors:\n{err_block}\n\n"
        f"위 모든 오류를 수정해 같은 spec 으로 재생성. "
        f"schema 의 모든 required 필드 + pattern 제약을 엄수.\n\n"
        f"---\n\n"
        f"{original_prompt}"
    )


async def validate_and_retry(
    content: str,
    spec: dict,
    model_adapter: Any,
    model: str,
    original_prompt: str,
    system_prompt: str = "",
    max_tokens: int = 8000,
) -> tuple[str, ValidationResult]:
    """spec.output_schema 기반 검증 + retry_with_feedback.

    Returns:
        (final_content, final_validation_result)
        - validation.pass_ True 면 정상
        - False 면 max_retries 소진 — 호출자가 NEEDS_HUMAN 처리 가능
    """
    schema_cfg = spec.get("output_schema") or {}
    schema_ref = schema_cfg.get("schema_ref")
    if not schema_ref or not schema_cfg.get("strict"):
        return content, ValidationResult(pass_=True)

    on_fail = schema_cfg.get("on_fail", "warn")
    max_retries = int(schema_cfg.get("max_retries", 1))

    validation = validate_against_schema(content, schema_ref)
    if validation.pass_:
        return content, validation

    logger.warning(
        "schema_validation_fail spec=%s schema=%s errors=%d on_fail=%s",
        spec.get("name", "?"), schema_ref, len(validation.errors), on_fail,
    )

    if on_fail != "retry_with_feedback":
        return content, validation

    for attempt in range(max_retries):
        retry_prompt = build_retry_prompt(
            original_prompt, validation.errors, schema_ref,
        )
        logger.info(
            "schema_retry attempt=%d/%d spec=%s",
            attempt + 1, max_retries, spec.get("name", "?"),
        )
        try:
            response = await model_adapter.call(
                model=model,
                prompt=retry_prompt,
                max_tokens=max_tokens,
                system=system_prompt,
            )
            content = getattr(response, "content", str(response))
        except Exception as e:
            logger.warning("schema_retry_call_fail attempt=%d err=%s", attempt + 1, e)
            return content, validation
        validation = validate_against_schema(content, schema_ref)
        if validation.pass_:
            logger.info(
                "schema_retry_pass attempt=%d spec=%s",
                attempt + 1, spec.get("name", "?"),
            )
            return content, validation

    logger.warning(
        "schema_retry_exhausted spec=%s errors=%d",
        spec.get("name", "?"), len(validation.errors),
    )
    return content, validation
