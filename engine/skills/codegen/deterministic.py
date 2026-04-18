"""
Stage 7: Deterministic Generation 확대 — 엔트리포인트.

원칙: AI 호출 이전에 **결정론적**으로 생성 가능한 부분을 최대한 코드로 처리.
기존 codegen/ 모듈(react/vue/db_schema/backend_api/frontend_infra) 을 최상위에서
dispatch 해 spec 에 따라 자동 선택.

Feature flag: V8_DETERMINISTIC=0 → 모든 deterministic 경로 bypass (AI 에게 위임).

사용 지점 (executor.py chunk 루프 전에 호출):
    result = try_deterministic(spec, node, context)
    if result:
        return result    # AI 호출 생략
    # else: 기존 LLM 경로 진행
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_DETERMINISTIC", "1") != "0"


def _match_spec_to_codegen(spec_name: str) -> str | None:
    """spec 이름을 기반으로 가능한 codegen 모듈 선택.

    반환값은 'react'|'vue'|'db_schema'|'backend_api'|'frontend_infra'|None.
    None 이면 deterministic 경로 없음 → AI 경로 fallback.
    """
    if not spec_name:
        return None
    low = spec_name.lower()
    # DB 스키마
    if ("db" in low and ("스키마" in spec_name or "schema" in low)) \
            or "erd" in low or "migration" in low or "prisma" in low:
        return "db_schema"
    # Backend API
    if ("api" in low and ("설계" in spec_name or "spec" in low or "router" in low)):
        return "backend_api"
    # Frontend infra (라우터/스토어/프로바이더)
    if ("라우터" in spec_name or "router" in low
            or "store" in low or "provider" in low):
        return "frontend_infra"
    # React/Vue scaffold (페이지/컴포넌트)
    if ("페이지" in spec_name or "page" in low or "화면" in spec_name):
        return "react"  # 기본 React, domain_profile 에서 override 가능
    return None


def try_deterministic(
    spec: dict,
    node: Any,
    context: dict | None = None,
    framework: str = "react",
) -> dict | None:
    """deterministic 경로 시도. 성공 시 {'content': ..., 'meta': ...}, 실패 시 None.

    - AI 호출 비용 0.
    - 적합한 spec 타입(프레임워크 scaffold/DB migration/API router 등) 이 아니면 None.
    - 실제 템플릿 생성은 기존 codegen/*.py 의 함수 재사용.
    """
    if not _ENABLED or not isinstance(spec, dict):
        return None

    spec_name = spec.get("name", "")
    target = _match_spec_to_codegen(spec_name)
    if not target:
        return None

    # AI 가 여전히 담당하는 영역은 스킵
    ai_required = spec.get("ai_required_sections") or []
    if ai_required:
        # spec 이 명시적으로 AI 필요 섹션을 지정했으면 deterministic 스킵
        logger.debug(
            "deterministic_skip spec=%s ai_required=%s",
            spec_name, ai_required,
        )
        return None

    context = context or {}
    try:
        if target == "db_schema":
            from engine.skills.codegen import db_schema as _db
            # _build_db_schema_code 기본 progress 제공
            code = _db._build_db_schema_code(context, spec)  # type: ignore[attr-defined]
            if code:
                return {
                    "content": code,
                    "meta": {"deterministic": True, "generator": "db_schema"},
                }
        elif target == "backend_api":
            from engine.skills.codegen import backend_api as _ba
            code = _ba._build_backend_api_code(context, spec)  # type: ignore[attr-defined]
            if code:
                return {
                    "content": code,
                    "meta": {"deterministic": True, "generator": "backend_api"},
                }
        elif target == "frontend_infra":
            from engine.skills.codegen import frontend_infra as _fi
            # frontend_infra 모듈은 여러 보조 함수 — 단일 entry 없음. 보류.
            return None
        elif target == "react":
            # react 는 페이지당 구체 레시피 필요 → D6 executor 훅 에서 호출.
            return None
    except AttributeError as e:
        # 기존 함수 시그니처 불일치 — 방어적 fallback
        logger.debug("deterministic_attr_error target=%s err=%s", target, e)
        return None
    except Exception as e:
        logger.warning("deterministic_generator_fail target=%s err=%s",
                       target, str(e)[:150])
        return None

    return None
