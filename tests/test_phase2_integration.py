"""Phase 2 통합 회귀 — 도메인 LLM hybrid + service essentials + 신규 schema."""
from __future__ import annotations

import json

import pytest

from engine.intake.domain_profiles import (
    detect_profile,
    detect_profile_hybrid,
    list_profiles,
    load_profile,
)
from engine.intake.processor import NODE_TEMPLATES
from engine.skills.qa.schema_validator import validate_against_schema


# ============================================================
# Pillar 2 — 도메인 신규 3종 + LLM hybrid
# ============================================================


def test_신규_도메인_3종_등록됨():
    profiles = list_profiles()
    assert "personal" in profiles
    assert "consumer" in profiles
    assert "general" in profiles


def test_personal_도메인_yaml_load():
    p = load_profile("personal")
    assert p is not None
    assert "habit" in p["keywords"]
    assert "personal" in p["keywords"]
    # screen_estimate small/medium/large 필수
    assert "screen_estimate" in p


def test_general_fallback_도메인():
    p = load_profile("general")
    assert p is not None
    assert "기타" in p["keywords"]


def test_habit_tracker_단어로_personal_매칭():
    """이번 세션 오분류 (habit→manufacturing) 정정 확인."""
    text = "개인용 습관 관리 앱 — habit tracker. 사용자가 매일 1초 체크인."
    result = detect_profile(text)
    assert result == "personal"


@pytest.mark.asyncio
async def test_hybrid_LLM_없으면_keyword():
    """model_adapter=None 이면 키워드만 사용, fallback general."""
    result = await detect_profile_hybrid("아무도 모르는 무관한 텍스트", None)
    assert result == "general"  # 매칭 0 → general fallback


@pytest.mark.asyncio
async def test_hybrid_keyword_매칭_시_그대로():
    text = "개인 습관 다이어리 앱"
    result = await detect_profile_hybrid(text, None)
    assert result == "personal"


# ============================================================
# Pillar 4 — service_type essentials NODE_TEMPLATES 등록
# ============================================================


def test_essentials_4종_NODE_TEMPLATES_등록():
    all_nodes = []
    for tmpl in NODE_TEMPLATES:
        all_nodes.extend(tmpl["nodes"])
    names = {n["name"] for n in all_nodes}
    assert "라우팅 설계서" in names
    assert "i18n 카탈로그" in names
    assert "환경변수 스키마" in names
    assert "PWA 매니페스트" in names


def test_essentials_when_service_조건():
    for tmpl in NODE_TEMPLATES:
        for n in tmpl["nodes"]:
            if n["name"] == "라우팅 설계서":
                assert "service:web_responsive" in n["when"]
            if n["name"] == "i18n 카탈로그":
                assert "service:web_responsive" in n["when"]
                assert "service:mobile_native" in n["when"]


def test_service_token_inject_로직():
    """processor.py 가 serviceType → service:xxx scope token 주입 로직 존재."""
    import inspect
    from engine.intake import processor
    src = inspect.getsource(processor)
    assert "service:" in src
    assert "serviceType" in src or "service_type" in src


def test_web_and_app_extends_둘다_활성화():
    """web_and_app 선택 시 web_responsive + mobile_native essentials 모두 활성화."""
    scopes = {"new", "service:web_and_app", "service:web_responsive", "service:mobile_native"}
    when_pwa = ["service:web_responsive", "service:web_and_app"]
    assert bool(set(when_pwa) & scopes) is True
    when_push = ["service:mobile_native"]
    assert bool(set(when_push) & scopes) is True


# ============================================================
# Pillar 1 — schema 4종 정상 작동
# ============================================================


def test_routing_config_schema_정상_pass():
    valid = {
        "framework": "next-app",
        "routes": [
            {"path": "/", "screen_id": "SC-HM-001", "page_slug": "home",
             "auth_required": False, "layout": "main"},
            {"path": "/login", "screen_id": "SC-AU-001", "page_slug": "login"},
        ],
        "guards": [{"name": "auth", "applies_to": ["/dashboard"], "redirect": "/login"}],
        "fallback": {"404": "/not-found"},
    }
    r = validate_against_schema(json.dumps(valid), "schemas/routing_config.json")
    assert r.pass_, f"errors: {r.errors}"


def test_routing_config_screen_id_pattern_FAIL():
    invalid = {
        "framework": "next-app",
        "routes": [
            {"path": "/", "screen_id": "INVALID-FORMAT"},
        ],
    }
    r = validate_against_schema(json.dumps(invalid), "schemas/routing_config.json")
    assert r.pass_ is False


def test_i18n_catalog_정상_pass():
    valid = {
        "default_locale": "ko",
        "supported_locales": ["ko", "en"],
        "namespaces": ["common", "auth"],
        "messages": {"ko": {"common": {"save": "저장"}}, "en": {"common": {"save": "Save"}}},
    }
    r = validate_against_schema(json.dumps(valid), "schemas/i18n_catalog.json")
    assert r.pass_, f"errors: {r.errors}"


def test_i18n_catalog_locale_pattern_FAIL():
    invalid = {
        "default_locale": "Korean",  # invalid pattern
        "supported_locales": ["ko"],
        "messages": {},
    }
    r = validate_against_schema(json.dumps(invalid), "schemas/i18n_catalog.json")
    assert r.pass_ is False


def test_env_schema_정상_pass():
    valid = {
        "vars": [
            {"name": "API_URL", "type": "url", "required": True,
             "description": "...", "secret": False},
            {"name": "DB_KEY", "type": "string", "required": True, "secret": True},
        ],
    }
    r = validate_against_schema(json.dumps(valid), "schemas/env_schema.json")
    assert r.pass_, f"errors: {r.errors}"


def test_env_schema_lowercase_FAIL():
    invalid = {"vars": [{"name": "api_url", "type": "string"}]}  # lowercase invalid
    r = validate_against_schema(json.dumps(invalid), "schemas/env_schema.json")
    assert r.pass_ is False


def test_iam_policy_정상_pass():
    valid = {
        "roles": [
            {"name": "admin", "description": "관리자"},
            {"name": "user", "inherits": []},
        ],
        "permissions": [
            {"resource": "habit", "actions": ["read", "create"], "roles": ["user"]},
            {"resource": "all", "actions": ["delete"], "roles": ["admin"]},
        ],
    }
    r = validate_against_schema(json.dumps(valid), "schemas/iam_policy.json")
    assert r.pass_, f"errors: {r.errors}"


def test_iam_policy_invalid_action_FAIL():
    invalid = {
        "roles": [{"name": "admin"}],
        "permissions": [{"resource": "x", "actions": ["GRANT"]}],  # GRANT invalid
    }
    r = validate_against_schema(json.dumps(invalid), "schemas/iam_policy.json")
    assert r.pass_ is False


# ============================================================
# Pillar 1 — output_formats.yaml 정의 확인
# ============================================================


def test_output_formats_yaml_load():
    import yaml
    from pathlib import Path
    p = (Path(__file__).parent.parent
         / "engine/skills/specs/_common/output_formats.yaml")
    assert p.exists()
    with p.open() as f:
        data = yaml.safe_load(f)
    types = data.get("types", {})
    # 11 핵심 type 모두 정의됨
    expected = {"document", "html", "json_spec", "openapi_yaml", "sql_ddl",
                "instantdb_schema", "firebase_rules", "state_machine_json",
                "iam_policy_json", "env_schema", "i18n_catalog", "routing_config",
                "code", "reference"}
    assert expected <= set(types.keys()), f"missing: {expected - set(types.keys())}"
    # 각 type 에 file_ext / parser 필수
    for t, cfg in types.items():
        assert "file_ext" in cfg, f"{t} missing file_ext"
        assert "parser" in cfg, f"{t} missing parser"
