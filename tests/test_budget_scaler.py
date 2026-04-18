"""
tests/test_budget_scaler.py  (V10)
Phase 예산 동적 스케일링 단위 테스트.
"""
from __future__ import annotations

import pytest

from engine.intake.size_estimator import SizeProfile, estimate_size
from engine.core.budget_scaler import (
    BASE_BUDGET,
    MAX_SIZE_FACTOR,
    MIN_SIZE_FACTOR,
    TYPE_FACTOR,
    _size_factor,
    scale_engagement_budget,
)


def test_default_profile_has_sane_output():
    profile = SizeProfile()
    result = scale_engagement_budget(profile)
    assert set(result.keys()) == set(BASE_BUDGET.keys())
    # 모든 phase 가 양수
    for phase, limit in result.items():
        assert limit > 0, f"{phase} 한도 0 이하"


def test_app_project_design_heavy():
    """앱 프로젝트는 DESIGN 가중치가 1.8배 이상 작용해야 함."""
    profile = SizeProfile(screens_est=15, features=10, project_type="app")
    result = scale_engagement_budget(profile)
    # app 의 DESIGN factor = 1.8 × size_factor
    assert result["DESIGN"] > BASE_BUDGET["DESIGN"] * 1.5
    assert result["DELIVER"] < BASE_BUDGET["DELIVER"]  # app 은 DELIVER 축소


def test_si_project_build_heavy():
    """SI 프로젝트는 BUILD 가중치가 2.5배."""
    profile = SizeProfile(project_type="si")
    result = scale_engagement_budget(profile)
    # si 의 BUILD factor = 2.5 × size_factor(default≈1.0)
    assert result["BUILD"] >= int(BASE_BUDGET["BUILD"] * 2.0)


def test_mlops_project_verify_heavy():
    """MLOps 는 VERIFY 가 2.0배."""
    profile = SizeProfile(project_type="mlops")
    result = scale_engagement_budget(profile)
    assert result["VERIFY"] >= int(BASE_BUDGET["VERIFY"] * 1.8)


def test_size_factor_upper_clamp():
    """screens_est 매우 많을 때 MAX_SIZE_FACTOR 상한 적용."""
    profile = SizeProfile(screens_est=100, features=50, project_type="app")
    sf = _size_factor(profile)
    assert sf == MAX_SIZE_FACTOR
    result = scale_engagement_budget(profile)
    assert result["DESIGN"] <= int(BASE_BUDGET["DESIGN"] * 1.8 * MAX_SIZE_FACTOR) + 1


def test_size_factor_lower_clamp():
    """매우 작은 프로젝트도 하한 적용."""
    profile = SizeProfile(screens_est=1, features=1, project_type="mixed")
    sf = _size_factor(profile)
    assert sf == MIN_SIZE_FACTOR


def test_habit_tracker_resimulation():
    """
    Habit Tracker 실제 데이터 역시뮬레이션.
    V9 실측: DESIGN 1,029K. V10 Level 1 한도는 >= 2,000K 여야 여유.
    """
    profile = SizeProfile(
        screens_est=15,
        features=9,
        categories=5,
        user_scenarios=3,
        project_type="app",
    )
    result = scale_engagement_budget(profile)
    assert result["DESIGN"] >= 2_000_000, (
        f"Habit Tracker DESIGN 한도 2M 미달: {result['DESIGN']:,}"
    )


def test_fallback_project_type_mixed():
    """알 수 없는 project_type 은 mixed 로 안전 fallback."""
    profile = SizeProfile(project_type="UNKNOWN")  # type: ignore
    # scale_engagement_budget 내부에서 TYPE_FACTOR.get 의 default 가 mixed
    result = scale_engagement_budget(profile)
    # mixed 는 DESIGN 1.4x 이므로 기본값 이상
    assert result["DESIGN"] >= BASE_BUDGET["DESIGN"]


def test_estimate_size_from_habit_tracker_like_raw():
    """실제 intake 형식 raw dict → SizeProfile 추출 흐름 검증."""
    raw = {
        "projectName": "마이 루틴",
        "serviceType": ["web_and_app"],
        "scope": ["prediction"],
        "features": ["a", "b", "c", "d", "e", "f", "g", "h", "i"],
        "userScenarios": ["s1", "s2", "s3"],
        "keyScreens": "List, Detail, Add, Analysis, Settings",
    }
    profile = estimate_size(raw)
    assert profile.project_type == "app"
    assert profile.features >= 9
    assert profile.screens_est >= 10
