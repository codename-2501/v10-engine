"""
v10 컴포넌트 조합 모델 — 통합 테스트.

registry (DB CRUD) + renderer (HTML 조립) + 라운드트립 검증.
"""

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Minimal async DB adapter (테스트용)
# ---------------------------------------------------------------------------

class _TestDB:
    """sqlite3 동기 래퍼 → async 인터페이스 모킹."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        # FK 제약 없이 composition 테이블만 생성 (테스트용)
        self.conn.executescript("""
            CREATE TABLE composition_tokens (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                data TEXT NOT NULL, content_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id)
            );
            CREATE TABLE composition_components (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id, name)
            );
            CREATE TABLE composition_recipes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                page_slug TEXT NOT NULL, page_name TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id, page_slug)
            );
        """)

    async def fetchone(self, query, params=()):
        cur = self.conn.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetchall(self, query, params=()):
        cur = self.conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    async def execute(self, query, params=()):
        self.conn.execute(query, params)
        self.conn.commit()
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return _TestDB()


@pytest.fixture
def project_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Registry 테스트
# ---------------------------------------------------------------------------

class TestCompositionRegistry:

    @pytest.mark.asyncio
    async def test_save_and_load_tokens(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, DesignTokens

        registry = CompositionRegistry(db)
        tokens = DesignTokens(
            project_id=project_id,
            colors={"primary": "#2563EB", "bg": "#111318", "text": "#eaedf2"},
            typography={"font_family": "Pretendard", "body_size": "16px"},
            spacing={"sm": "8px", "md": "16px"},
        )
        token_id = await registry.save_tokens(tokens)
        assert token_id

        loaded = await registry.load_tokens(project_id)
        assert loaded is not None
        assert loaded.colors["primary"] == "#2563EB"
        assert loaded.typography["font_family"] == "Pretendard"
        assert loaded.version == 1

    @pytest.mark.asyncio
    async def test_tokens_version_increment(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, DesignTokens

        registry = CompositionRegistry(db)
        t1 = DesignTokens(project_id=project_id, colors={"primary": "#111"})
        await registry.save_tokens(t1)

        t2 = DesignTokens(project_id=project_id, colors={"primary": "#222"})
        await registry.save_tokens(t2)

        loaded = await registry.load_tokens(project_id)
        assert loaded.version == 2
        assert loaded.colors["primary"] == "#222"

    @pytest.mark.asyncio
    async def test_save_and_load_component(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, Component

        registry = CompositionRegistry(db)
        comp = Component(
            name="hero_header",
            category="layout",
            html_template='<header class="hero"><h1>{{title}}</h1></header>',
            css='.hero { background: var(--color-bg); }',
            slots={"title": {"type": "text", "required": True}},
        )
        comp_id = await registry.save_component(project_id, comp)
        assert comp_id

        loaded = await registry.load_component(project_id, "hero_header")
        assert loaded is not None
        assert loaded.category == "layout"
        assert "{{title}}" in loaded.html_template

    @pytest.mark.asyncio
    async def test_load_all_components(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, Component

        registry = CompositionRegistry(db)
        for name in ["header", "card", "footer"]:
            comp = Component(name=name, category="layout")
            await registry.save_component(project_id, comp)

        all_comps = await registry.load_all_components(project_id)
        assert len(all_comps) == 3

    @pytest.mark.asyncio
    async def test_save_and_load_recipe(self, db, project_id):
        from engine.composition.registry import (
            CompositionRegistry, PageRecipe, ComponentPlacement, SlotBinding
        )

        registry = CompositionRegistry(db)
        recipe = PageRecipe(
            project_id=project_id,
            page_name="메인 대시보드",
            page_slug="main-dashboard",
            title="대시보드",
            layout="sidebar-left",
            placements=[
                ComponentPlacement(
                    component_name="stat_card",
                    order=1,
                    bindings=[SlotBinding(slot_name="value", value="1,234")],
                ),
            ],
        )
        recipe_id = await registry.save_recipe(recipe)
        assert recipe_id

        loaded = await registry.load_recipe(project_id, "main-dashboard")
        assert loaded is not None
        assert loaded.page_name == "메인 대시보드"
        assert len(loaded.placements) == 1
        assert loaded.placements[0].bindings[0].value == "1,234"

    @pytest.mark.asyncio
    async def test_tokens_changed(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, DesignTokens

        registry = CompositionRegistry(db)
        tokens = DesignTokens(project_id=project_id, colors={"primary": "#aaa"})
        await registry.save_tokens(tokens)

        assert await registry.tokens_changed(project_id, "wrong_hash") is True
        assert await registry.tokens_changed(project_id, tokens.content_hash()) is False

    @pytest.mark.asyncio
    async def test_get_component_names(self, db, project_id):
        from engine.composition.registry import CompositionRegistry, Component

        registry = CompositionRegistry(db)
        for name in ["beta", "alpha", "gamma"]:
            await registry.save_component(project_id, Component(name=name))

        names = await registry.get_component_names(project_id)
        assert names == ["alpha", "beta", "gamma"]  # 정렬됨


# ---------------------------------------------------------------------------
# Renderer 테스트
# ---------------------------------------------------------------------------

class TestCompositionRenderer:

    @pytest.mark.asyncio
    async def test_render_page_basic(self, db, project_id):
        from engine.composition.registry import (
            CompositionRegistry, DesignTokens, Component, PageRecipe,
            ComponentPlacement, SlotBinding,
        )
        from engine.composition.renderer import CompositionRenderer

        registry = CompositionRegistry(db)

        # 토큰 저장
        tokens = DesignTokens(
            project_id=project_id,
            colors={"primary": "#C17840", "bg": "#111318", "text": "#eaedf2"},
            typography={"font_family": "Pretendard", "body_size": "16px", "heading_scale": [2.5, 2, 1.5]},
            spacing={"md": "16px"},
            effects={"radius_md": "8px"},
        )
        await registry.save_tokens(tokens)

        # 컴포넌트 저장
        header = Component(
            name="page_header",
            category="layout",
            html_template='<header><h1>{{title}}</h1><p>{{subtitle}}</p></header>',
            css='header { background: var(--color-bg); padding: var(--space-md); }',
        )
        card = Component(
            name="content_card",
            category="content",
            html_template='<div class="card"><h3>{{heading}}</h3><p>{{body}}</p></div>',
            css='.card { background: var(--color-surface, #1a1d24); border-radius: var(--radius-md); }',
        )
        await registry.save_component(project_id, header)
        await registry.save_component(project_id, card)

        # 레시피 저장
        recipe = PageRecipe(
            project_id=project_id,
            page_name="홈",
            page_slug="home",
            title="홈페이지",
            layout="single-column",
            placements=[
                ComponentPlacement(
                    component_name="page_header", order=1,
                    bindings=[
                        SlotBinding(slot_name="title", value="실버케어센터"),
                        SlotBinding(slot_name="subtitle", value="따뜻한 돌봄"),
                    ],
                ),
                ComponentPlacement(
                    component_name="content_card", order=2,
                    bindings=[
                        SlotBinding(slot_name="heading", value="서비스 안내"),
                        SlotBinding(slot_name="body", value="전문 요양 서비스를 제공합니다."),
                    ],
                ),
            ],
        )
        await registry.save_recipe(recipe)

        # 렌더링
        renderer = CompositionRenderer(registry)
        result = await renderer.render_page(project_id, "home")

        assert result.html
        assert "<!DOCTYPE html>" in result.html
        assert "실버케어센터" in result.html
        assert "따뜻한 돌봄" in result.html
        assert "서비스 안내" in result.html
        assert "--color-primary" in result.html
        assert result.content_hash
        assert result.page_name == "홈"
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_render_with_repeat(self, db, project_id):
        from engine.composition.registry import (
            CompositionRegistry, DesignTokens, Component, PageRecipe,
            ComponentPlacement, SlotBinding,
        )
        from engine.composition.renderer import CompositionRenderer

        registry = CompositionRegistry(db)
        await registry.save_tokens(DesignTokens(project_id=project_id))

        list_item = Component(
            name="list_item",
            category="data",
            html_template='<li>{{_item.name}} - {{_item.role}}</li>',
        )
        await registry.save_component(project_id, list_item)

        recipe = PageRecipe(
            project_id=project_id, page_name="팀", page_slug="team",
            placements=[
                ComponentPlacement(
                    component_name="list_item", order=1,
                    repeat="members",
                ),
            ],
        )
        await registry.save_recipe(recipe)

        renderer = CompositionRenderer(registry)
        result = await renderer.render_page(project_id, "team", data_context={
            "members": [
                {"name": "김철수", "role": "관리자"},
                {"name": "이영희", "role": "간호사"},
                {"name": "박지민", "role": "사회복지사"},
            ]
        })

        assert "김철수" in result.html
        assert "이영희" in result.html
        assert "박지민" in result.html

    @pytest.mark.asyncio
    async def test_render_missing_recipe(self, db, project_id):
        from engine.composition.registry import CompositionRegistry
        from engine.composition.renderer import CompositionRenderer

        registry = CompositionRegistry(db)
        renderer = CompositionRenderer(registry)
        result = await renderer.render_page(project_id, "nonexistent")

        assert "렌더링 오류" in result.html
        assert len(result.warnings) > 0

    @pytest.mark.asyncio
    async def test_diff_impact_tokens(self, db, project_id):
        from engine.composition.registry import (
            CompositionRegistry, PageRecipe, ComponentPlacement,
        )
        from engine.composition.renderer import CompositionRenderer

        registry = CompositionRegistry(db)
        for slug in ["page-a", "page-b", "page-c"]:
            recipe = PageRecipe(
                project_id=project_id, page_name=slug, page_slug=slug,
                placements=[ComponentPlacement(component_name="card", order=1)],
            )
            await registry.save_recipe(recipe)

        renderer = CompositionRenderer(registry)
        affected = await renderer.diff_impact(project_id, "tokens")
        assert len(affected) == 3  # 토큰 변경 → 모든 페이지

    @pytest.mark.asyncio
    async def test_diff_impact_component(self, db, project_id):
        from engine.composition.registry import (
            CompositionRegistry, PageRecipe, ComponentPlacement,
        )
        from engine.composition.renderer import CompositionRenderer

        registry = CompositionRegistry(db)

        r1 = PageRecipe(
            project_id=project_id, page_name="A", page_slug="a",
            placements=[ComponentPlacement(component_name="header", order=1)],
        )
        r2 = PageRecipe(
            project_id=project_id, page_name="B", page_slug="b",
            placements=[ComponentPlacement(component_name="footer", order=1)],
        )
        await registry.save_recipe(r1)
        await registry.save_recipe(r2)

        renderer = CompositionRenderer(registry)
        affected = await renderer.diff_impact(project_id, "header")
        assert affected == ["a"]  # header는 페이지 A에만 사용


# ---------------------------------------------------------------------------
# 슬롯 렌더링 단위 테스트
# ---------------------------------------------------------------------------

class TestSlotRendering:

    def test_simple_slots(self):
        from engine.composition.renderer import _render_slots
        result = _render_slots("<h1>{{title}}</h1>", {"title": "Hello"})
        assert "<h1>Hello</h1>" == result

    def test_conditional_true(self):
        from engine.composition.renderer import _render_slots
        result = _render_slots("{{#if show}}<b>YES</b>{{/if}}", {"show": True})
        assert "<b>YES</b>" in result

    def test_conditional_false(self):
        from engine.composition.renderer import _render_slots
        result = _render_slots("{{#if show}}<b>YES</b>{{/if}}", {"show": False})
        assert "<b>YES</b>" not in result

    def test_each_block(self):
        from engine.composition.renderer import _render_slots
        result = _render_slots(
            "{{#each items}}<li>{{_item}}</li>{{/each}}",
            {"items": ["A", "B", "C"]}
        )
        assert "<li>A</li>" in result
        assert "<li>B</li>" in result
        assert "<li>C</li>" in result

    def test_nested_path(self):
        from engine.composition.renderer import _resolve_path
        data = {"a": {"b": {"c": 42}}}
        assert _resolve_path("a.b.c", data) == 42

    def test_array_index_path(self):
        from engine.composition.renderer import _resolve_path
        data = {"items": [{"name": "X"}]}
        assert _resolve_path("items[0].name", data) == "X"

    def test_transform_number_format(self):
        from engine.composition.renderer import _apply_transform
        assert _apply_transform(1234567, "number_format") == "1,234,567"

    def test_transform_date_format(self):
        from engine.composition.renderer import _apply_transform
        assert _apply_transform("2026-03-28", "date_format") == "2026년 3월 28일"

    def test_transform_truncate(self):
        from engine.composition.renderer import _apply_transform
        assert _apply_transform("hello world", "truncate:5") == "hello..."

    def test_condition_comparison(self):
        from engine.composition.renderer import _evaluate_condition
        assert _evaluate_condition("if data.count > 0", {"data": {"count": 5}}) is True
        assert _evaluate_condition("if data.count > 10", {"data": {"count": 5}}) is False

    def test_condition_unless(self):
        from engine.composition.renderer import _evaluate_condition
        assert _evaluate_condition("unless data.empty", {"data": {"empty": False}}) is True
        assert _evaluate_condition("unless data.empty", {"data": {"empty": True}}) is False


# ---------------------------------------------------------------------------
# CSS 생성 테스트
# ---------------------------------------------------------------------------

class TestDesignTokensCSS:

    def test_to_css_variables(self):
        from engine.composition.registry import DesignTokens
        tokens = DesignTokens(
            colors={"primary": "#C17840", "bg": "#111318"},
            typography={"font_family": "Pretendard", "body_size": "16px", "heading_scale": [2.5, 2]},
            spacing={"sm": "8px"},
            effects={"shadow_sm": "0 1px 2px rgba(0,0,0,.05)"},
        )
        css = tokens.to_css_variables()
        assert ":root {" in css
        assert "--color-primary: #C17840;" in css
        assert "--font-family: 'Pretendard', sans-serif;" in css
        assert "--font-size-h1: 2.5rem;" in css
        assert "--space-sm: 8px;" in css
        assert "--shadow-sm: 0 1px 2px rgba(0,0,0,.05);" in css

    def test_content_hash_changes(self):
        from engine.composition.registry import DesignTokens
        t1 = DesignTokens(colors={"primary": "#aaa"})
        t2 = DesignTokens(colors={"primary": "#bbb"})
        assert t1.content_hash() != t2.content_hash()

    def test_content_hash_stable(self):
        from engine.composition.registry import DesignTokens
        t1 = DesignTokens(colors={"primary": "#aaa"})
        assert t1.content_hash() == t1.content_hash()
