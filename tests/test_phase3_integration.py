"""Phase 3 회귀 — cross-ref hook + 의존 순서 + 신규 schema."""
from __future__ import annotations

import json

from engine.intake.processor import INTRA_PHASE_DEPS
from engine.skills.qa.schema_validator import validate_against_schema


# ============================================================
# Pillar 3 — 의존 순서 (INTRA_PHASE_DEPS) 강제
# ============================================================


def test_essentials_의존_추가됨():
    """Phase 4 essentials 가 적절한 upstream 노드에 의존."""
    deps = set(INTRA_PHASE_DEPS)
    # 라우팅 설계서 ← 화면 목록 정의서 / 페이지 레시피
    assert ("화면 목록 정의서", "라우팅 설계서") in deps
    assert ("페이지 레시피", "라우팅 설계서") in deps
    # i18n ← 화면 설계서 / UI 시안
    assert ("화면 설계서 (와이어프레임+스토리보드)", "i18n 카탈로그") in deps
    # env ← HLD / API 설계서
    assert ("시스템 아키텍처 설계서 (HLD)", "환경변수 스키마") in deps


def test_InstantDB_의존_추가됨():
    deps = set(INTRA_PHASE_DEPS)
    assert ("DB 설계서 (ERD·테이블 정의)", "InstantDB 데이터 모델 설계") in deps
    assert ("보안 설계서 (ISMS)", "InstantDB 권한 정책 설계") in deps


def test_페이지_레시피_라이브러리_의존_보존():
    """기존 컴포넌트 chain (라이브러리 → 레지스트리 → 레시피) 회귀 보호."""
    deps = set(INTRA_PHASE_DEPS)
    assert ("디자인 토큰", "컴포넌트 라이브러리") in deps
    assert ("컴포넌트 라이브러리", "컴포넌트 레지스트리") in deps
    assert ("컴포넌트 레지스트리", "페이지 레시피") in deps


# ============================================================
# Pillar 1 — 신규 schema 4종
# ============================================================


def test_component_library_item_정상_pass():
    valid = {
        "name": "kpi_card",
        "category": "dashboard",
        "html_template": "<div class='kpi-card'>{{value}}</div>",
        "css": ".kpi-card { padding: 16px; }",
        "slots": [
            {"name": "value", "type": "string", "required": True},
        ],
    }
    r = validate_against_schema(
        json.dumps(valid), "schemas/component_library_item.json"
    )
    assert r.pass_, f"errors: {r.errors}"


def test_component_library_item_name_dash_FAIL():
    invalid = {
        "name": "kpi-card",  # dash invalid (snake_case 강제)
        "category": "dashboard",
        "html_template": "<div></div>",
        "css": "",
    }
    r = validate_against_schema(
        json.dumps(invalid), "schemas/component_library_item.json"
    )
    assert r.pass_ is False


def test_page_recipe_정상_pass():
    valid = {
        "page_name": "홈",
        "page_slug": "home",
        "scr_id": "SC-HM-001",
        "layout": "single-column",
        "placements": [
            {
                "component_name": "page_header",
                "order": 1,
                "bindings": [{"slot_name": "title", "value": "홈"}],
            },
            {"component_name": "kpi_card", "order": 2},
        ],
    }
    r = validate_against_schema(json.dumps(valid), "schemas/page_recipe.json")
    assert r.pass_, f"errors: {r.errors}"


def test_page_recipe_component_name_pattern_FAIL():
    invalid = {
        "page_name": "홈",
        "page_slug": "home",
        "scr_id": "SC-HM-001",
        "placements": [
            {"component_name": "SC-HB-001"},  # 화면 ID 형식 invalid
        ],
    }
    r = validate_against_schema(json.dumps(invalid), "schemas/page_recipe.json")
    assert r.pass_ is False


def test_state_machine_정상_pass():
    valid = {
        "id": "checkinFlow",
        "initial": "idle",
        "states": {
            "idle": {"on": {"START": "active"}},
            "active": {"on": {"COMPLETE": "done"}},
            "done": {"type": "final"},
        },
    }
    r = validate_against_schema(
        json.dumps(valid), "schemas/state_machine.json"
    )
    assert r.pass_, f"errors: {r.errors}"


def test_state_machine_invalid_id_FAIL():
    invalid = {
        "id": "Check-In-Flow",  # dashes + Caps invalid
        "initial": "idle",
        "states": {"idle": {}},
    }
    r = validate_against_schema(
        json.dumps(invalid), "schemas/state_machine.json"
    )
    assert r.pass_ is False


def test_instantdb_schema_정상_pass():
    valid = {
        "entities": {
            "users": {
                "fields": {
                    "name": {"type": "string", "indexed": True},
                    "email": {"type": "string", "unique": True},
                },
            },
            "habits": {
                "fields": {
                    "title": {"type": "string"},
                    "createdAt": {"type": "date"},
                },
            },
        },
        "links": {
            "userHabits": {
                "forward": {"on": "users", "has": "many", "label": "habits"},
                "reverse": {"on": "habits", "has": "one", "label": "owner"},
            },
        },
    }
    r = validate_against_schema(
        json.dumps(valid), "schemas/instantdb_schema.json"
    )
    assert r.pass_, f"errors: {r.errors}"


def test_instantdb_schema_invalid_field_type_FAIL():
    invalid = {
        "entities": {
            "users": {"fields": {"x": {"type": "uuid"}}},  # uuid 미지원
        },
    }
    r = validate_against_schema(
        json.dumps(invalid), "schemas/instantdb_schema.json"
    )
    assert r.pass_ is False


# ============================================================
# Pillar 3 — cross-ref hook 시뮬레이션 (executor 통합 변수만 확인)
# ============================================================


def test_executor_recipe_post_hook_존재():
    """executor.py 에 cross_reference 호출 코드 존재."""
    import inspect
    from engine.skills import executor
    src = inspect.getsource(executor)
    assert "verify_component_consistency" in src
    assert "cross_ref_post_recipe" in src


def test_executor_recipe_hook_description_persist():
    """결과를 nodes.description 에 저장하는 로직 존재."""
    import inspect
    from engine.skills import executor
    src = inspect.getsource(executor)
    assert "cross_reference" in src
    assert "missing" in src
