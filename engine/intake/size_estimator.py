"""
engine/intake/size_estimator.py  (V10)

Intake raw_json → SizeProfile 추출.
budget_scaler.scale_engagement_budget() 의 입력으로 사용.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ProjectType = Literal["app", "si", "mlops", "data", "mixed"]


@dataclass
class SizeProfile:
    """프로젝트 규모 프로파일 (raw_json 에서 추출)."""
    screens_est: int = 10       # 예상 화면 수
    features: int = 5           # 요구 기능 수
    categories: int = 3         # 컴포넌트 카테고리 수
    user_scenarios: int = 3     # 사용자 시나리오 수
    integrations: int = 0       # 외부 시스템 연동 수
    project_type: ProjectType = "mixed"
    _raw_sample: str = ""       # 디버깅용


def _parse_list(value: Any) -> list:
    """raw_json 필드가 list/str/None 섞여 들어와도 list로 정규화."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        # 콤마 구분 문자열도 수용
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _classify_project_type(raw: dict) -> ProjectType:
    """
    raw_json 의 serviceType + scope + projectTypes 조합으로 프로젝트 타입 판정.

    규칙 (우선순위):
    1. mlops / AI 모델 관련 scope → mlops
    2. data_platform 또는 대용량 데이터 → data
    3. web_and_app / mobile_app / web_service → app
    4. api_service + 관공서/공공 → si
    5. 그 외 → mixed
    """
    service_types = set(_parse_list(raw.get("serviceType") or raw.get("service_type")))
    scopes = set(_parse_list(raw.get("scope")))
    project_types = set(_parse_list(raw.get("projectTypes") or raw.get("project_types")))

    # mlops 패턴
    if scopes & {"ai_model", "mlops", "llm"}:
        return "mlops"

    # data 플랫폼
    if scopes & {"data_pipeline"} or "data_platform" in service_types:
        return "data"

    # app 패턴
    if service_types & {"web_and_app", "mobile_app", "web_service"}:
        return "app"

    # SI/공공 패턴
    if scopes & {"si", "public", "consulting"}:
        return "si"

    return "mixed"


def _estimate_screens(raw: dict) -> int:
    """화면 수 추정 휴리스틱."""
    # 1. keyScreens 텍스트에서 숫자 + 언급된 화면명 개수
    key_screens = str(raw.get("keyScreens") or "")
    # 2. features 수 × 1.5 (feature당 약 1~2화면)
    features_n = len(_parse_list(raw.get("features")))
    # 3. userScenarios 수 (시나리오당 2~3화면)
    scenarios_n = len(_parse_list(raw.get("userScenarios") or raw.get("user_scenarios")))

    # 섹션 헤더 같은 화면 카운트 (":" 또는 "," 로 구분된 항목 수)
    key_screen_count = max(
        key_screens.count(",") + key_screens.count("/") + 1 if key_screens else 0,
        1 if key_screens else 0,
    )

    est = max(
        int(features_n * 1.5) + scenarios_n,
        key_screen_count,
        10,  # 최소 10개 가정
    )
    # 너무 큰 값 방지 (outlier clamp)
    return min(est, 200)


def estimate_size(raw_json: str | dict) -> SizeProfile:
    """
    intake raw_json → SizeProfile.

    raw 가 str 이면 json.loads. dict 면 그대로 사용.
    파싱 실패 시 기본 SizeProfile(project_type="mixed") 반환 (안전 fallback).
    """
    if isinstance(raw_json, str):
        try:
            raw = json.loads(raw_json)
        except (ValueError, TypeError):
            logger.warning("size_estimator_parse_failed — fallback to mixed/default")
            return SizeProfile(project_type="mixed")
    elif isinstance(raw_json, dict):
        raw = raw_json
    else:
        return SizeProfile(project_type="mixed")

    if not isinstance(raw, dict):
        return SizeProfile(project_type="mixed")

    features_n = len(_parse_list(raw.get("features")))
    scenarios_n = len(_parse_list(raw.get("userScenarios") or raw.get("user_scenarios")))
    integrations_raw = raw.get("integrations") or ""
    integrations_n = (
        len([x for x in str(integrations_raw).split(",") if x.strip()])
        if integrations_raw else 0
    )
    project_type = _classify_project_type(raw)
    screens_est = _estimate_screens(raw)

    # categories: scope 또는 projectTypes 의 항목 수를 근사로
    scopes = _parse_list(raw.get("scope"))
    categories = max(len(scopes), 3)

    profile = SizeProfile(
        screens_est=screens_est,
        features=max(features_n, 5),       # 최소 5
        categories=categories,
        user_scenarios=max(scenarios_n, 3),
        integrations=integrations_n,
        project_type=project_type,
        _raw_sample=str(raw.get("projectName") or "")[:60],
    )
    logger.info(
        "size_estimated name=%r type=%s screens=%d features=%d",
        profile._raw_sample, profile.project_type,
        profile.screens_est, profile.features,
    )
    return profile
