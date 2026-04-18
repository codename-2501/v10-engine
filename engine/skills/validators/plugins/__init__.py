"""
Stage 20: Validator Plugin System.

산출물 타입별 programmatic validator 플러그인. AI 호출 0.

사용 패턴 (executor.py 의 artifact 저장 직전):

    from engine.skills.validators.plugins import run_validator_chain, ChainResult
    result = run_validator_chain(content, spec, context)
    if result.fixed_content:
        content = result.fixed_content
    if result.failures:
        # spec.output_schema.on_fail 정책 따름
        ...

각 plugin 은 `Validator` 상속 후 `engine.skills.validators.plugins` 아래에 모듈
추가. `__init__.py` 에서 import 시 자동으로 REGISTRY 에 등록됨.
"""
from __future__ import annotations

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    ChainResult,
    REGISTRY,
    register,
    run_validator_chain,
)

# Plugin 자동 등록 (import side-effect)
# 각 plugin 파일이 추가되면 여기에도 import 행 추가.
from engine.skills.validators.plugins import id_unique          # noqa: F401
from engine.skills.validators.plugins import markdown_table     # noqa: F401
from engine.skills.validators.plugins import link_integrity     # noqa: F401
from engine.skills.validators.plugins import css_class_integrity  # noqa: F401
from engine.skills.validators.plugins import token_reference    # noqa: F401
from engine.skills.validators.plugins import mermaid            # noqa: F401
from engine.skills.validators.plugins import erd                # noqa: F401
from engine.skills.validators.plugins import openapi            # noqa: F401
from engine.skills.validators.plugins import semantic_structural  # noqa: F401

__all__ = [
    "Validator",
    "PluginValidationResult",
    "Fix",
    "ChainResult",
    "REGISTRY",
    "register",
    "run_validator_chain",
]
