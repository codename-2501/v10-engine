"""S13: domain profile 기반 component_categories override 단위 테스트."""
from __future__ import annotations

from engine.intake.domain_profiles import load_profile


def test_silvercare_profile_care_카테고리():
    p = load_profile("silvercare")
    assert p is not None
    cats = p.get("component_categories") or []
    names = [c.get("name") for c in cats]
    assert "care" in names
    # 돌봄 특화 컴포넌트 포함
    care_cat = next(c for c in cats if c.get("name") == "care")
    assert "care_journal" in care_cat["chunk_items"]


def test_manufacturing_profile_monitoring_카테고리():
    p = load_profile("manufacturing")
    cats = p.get("component_categories") or []
    names = [c.get("name") for c in cats]
    assert "monitoring" in names
    mon = next(c for c in cats if c.get("name") == "monitoring")
    assert any("kpi" in i or "gauge" in i for i in mon["chunk_items"])


def test_ecommerce_profile_commerce_카테고리():
    p = load_profile("ecommerce")
    cats = p.get("component_categories") or []
    names = [c.get("name") for c in cats]
    assert "commerce" in names
    commerce = next(c for c in cats if c.get("name") == "commerce")
    assert "cart_item" in commerce["chunk_items"]


def test_saas_profile_dashboard_collaboration():
    p = load_profile("saas")
    cats = p.get("component_categories") or []
    names = [c.get("name") for c in cats]
    assert "dashboard" in names
    assert "collaboration" in names


def test_finance_profile_trading_카테고리():
    p = load_profile("finance")
    cats = p.get("component_categories") or []
    names = [c.get("name") for c in cats]
    assert "trading" in names


def test_모든_프로파일_카테고리_보유():
    for name in ("silvercare", "ecommerce", "saas", "finance", "manufacturing"):
        p = load_profile(name)
        cats = p.get("component_categories") or []
        assert len(cats) >= 4, f"{name}: 카테고리 {len(cats)}개 부족"
        # 각 카테고리마다 chunk_items 있음
        for c in cats:
            assert isinstance(c.get("chunk_items"), list)
            assert len(c["chunk_items"]) >= 3, f"{name}/{c['name']}: chunk_items < 3"


def test_카테고리_구조_spec_과_호환():
    """splitting.py 가 기대하는 구조 (name + chunk_items) 호환."""
    p = load_profile("silvercare")
    for c in p["component_categories"]:
        # splitting.py 의 categories 로 바로 사용 가능해야 함
        assert "name" in c
        assert isinstance(c["name"], str)
        # chunk_items 는 splitting 은 필수 아니지만 S9 chunk_items_source 는 필요
        # (splitting 은 카테고리별 sub-node 만들고 chunk_items 는 executor 가 처리)
