"""domain_profiles 단위 테스트 — S3-4."""
from __future__ import annotations

from engine.intake.domain_profiles import detect_profile, list_profiles, load_profile


def test_5종_프로파일_존재():
    profiles = list_profiles()
    for name in ("silvercare", "ecommerce", "saas", "finance", "manufacturing"):
        assert name in profiles


def test_load_각_프로파일_필수필드():
    for name in list_profiles():
        p = load_profile(name)
        assert p is not None
        assert "name" in p
        assert "keywords" in p and len(p["keywords"]) >= 3
        assert "regulations" in p


def test_detect_silvercare():
    assert detect_profile("명성실버케어센터 디지털 전환") == "silvercare"


def test_detect_ecommerce():
    assert detect_profile("쇼핑몰 결제 장바구니") == "ecommerce"


def test_detect_finance():
    assert detect_profile("증권 시세 호가 거래") == "finance"


def test_detect_없음_None():
    assert detect_profile("일반 텍스트 아무것도 매치 안됨") is None


def test_load_없는_프로파일():
    assert load_profile("nonexistent") is None
