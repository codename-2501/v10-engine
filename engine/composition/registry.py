"""
engine/composition/registry.py
컴포넌트 조합 모델 레지스트리 — 디자인 토큰 · 컴포넌트 라이브러리 · 레시피 관리.

3계층 구조:
  1. DesignTokens  — CSS 변수, 색상, 타이포그래피, 간격 등
  2. ComponentLibrary — 재사용 가능한 HTML/CSS 컴포넌트 (헤더, 카드, 테이블 등)
  3. PageRecipe — 페이지별 조립 지시서 (어떤 컴포넌트를 어떤 순서/데이터로)

AI는 이 세 가지만 생성하고, 조립은 renderer.py가 수행 (AI 0회).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _hex_to_rgb(hex_color: str) -> str:
    """#RRGGBB → 'R, G, B' (CSS rgb() 용)."""
    h = hex_color.lstrip("#")
    if len(h) < 6:
        h = h[:3] * 2 if len(h) == 3 else h + "0" * (6 - len(h))
    try:
        return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"
    except ValueError:
        return "239, 68, 68"


# ---------------------------------------------------------------------------
# 1. 디자인 토큰
# ---------------------------------------------------------------------------

@dataclass
class DesignTokens:
    """프로젝트 전체에 적용되는 디자인 토큰.

    AI가 한 번 생성하면, 이후 토큰 변경만으로 전체 산출물 리스타일링 가능.
    """
    id: str = ""
    project_id: str = ""

    # 색상
    colors: Dict[str, str] = field(default_factory=dict)
    # 예: {"primary": "#2563EB", "secondary": "#7C3AED", "bg": "#FFFFFF", ...}

    # 타이포그래피
    typography: Dict[str, Any] = field(default_factory=dict)
    # 예: {"font_family": "Pretendard", "heading_scale": [2.5, 2, 1.5, 1.25, 1.1],
    #       "body_size": "16px", "line_height": 1.6}

    # 간격
    spacing: Dict[str, str] = field(default_factory=dict)
    # 예: {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "32px", "2xl": "48px"}

    # 반응형 브레이크포인트
    breakpoints: Dict[str, str] = field(default_factory=dict)
    # 예: {"mobile": "480px", "tablet": "768px", "desktop": "1024px", "wide": "1280px"}

    # 그림자, 둥근 모서리 등
    effects: Dict[str, str] = field(default_factory=dict)
    # 예: {"shadow_sm": "0 1px 2px rgba(0,0,0,0.05)", "radius_md": "8px"}

    # 프로젝트 메타 (AI가 톤/분위기 판단에 사용)
    meta: Dict[str, str] = field(default_factory=dict)
    # 예: {"tone": "따뜻하고 신뢰감 있는", "industry": "실버케어"}

    version: int = 1

    def to_css_variables(self) -> str:
        """토큰을 CSS 커스텀 속성(변수)으로 변환.

        기본 토큰 + 컴포넌트가 기대하는 시맨틱 변수 전체를 생성.
        """
        lines = [":root {"]

        c = self.colors
        # --- 기본 색상 토큰 ---
        for name, value in c.items():
            lines.append(f"  --color-{name}: {value};")

        # --- 시맨틱 색상 매핑 (컴포넌트 CSS가 참조하는 변수) ---
        primary = c.get("primary", "#2563EB")
        primary_dim = c.get("primary_dim", primary)
        secondary = c.get("secondary", "#7C3AED")
        bg = c.get("bg", "#ffffff")
        surface = c.get("surface", "#f8fafc")
        surface_hover = c.get("surface_hover", surface)
        text = c.get("text", "#1a1a2e")
        text_muted = c.get("text_muted", "#64748b")
        border = c.get("border", "#e2e8f0")
        success = c.get("success", "#22c55e")
        warning = c.get("warning", "#f59e0b")
        error = c.get("error", "#ef4444")
        info = c.get("info", "#3b82f6")

        # accent 계열
        lines.append(f"  --color-accent: {primary};")
        lines.append(f"  --color-accent-primary: {primary};")
        lines.append(f"  --color-accent-secondary: {secondary};")
        lines.append(f"  --color-accent-hover: {primary_dim};")
        lines.append(f"  --color-accent-subtle: {primary}22;")
        lines.append(f"  --color-accent-bg: {primary}11;")
        lines.append(f"  --color-accent-ring: {primary}44;")

        # text 계열
        lines.append(f"  --color-text-primary: {text};")
        lines.append(f"  --color-text-secondary: {text_muted};")
        lines.append(f"  --color-text-on-accent: #ffffff;")
        lines.append(f"  --color-text-inverse: {bg};")

        # bg/surface 계열
        lines.append(f"  --color-bg-primary: {bg};")
        lines.append(f"  --color-bg-surface: {surface};")
        lines.append(f"  --color-bg-card: {surface};")
        lines.append(f"  --color-surface-1: {surface};")
        lines.append(f"  --color-surface-2: {surface_hover};")
        lines.append(f"  --color-surface-3: {surface_hover};")

        # border
        lines.append(f"  --color-border-subtle: {border};")

        # primary hover
        lines.append(f"  --color-primary-hover: {primary_dim};")

        # danger
        lines.append(f"  --color-danger: {error};")
        lines.append(f"  --color-danger-bg: {error}11;")

        # status 계열
        lines.append(f"  --color-status-success: {success};")
        lines.append(f"  --color-status-warning: {warning};")
        lines.append(f"  --color-status-error: {error};")
        lines.append(f"  --color-status-info: {info};")

        # success 계열
        lines.append(f"  --color-success-bg: {success}15;")
        lines.append(f"  --color-success-subtle: {success}22;")
        lines.append(f"  --color-success-border: {success}44;")
        lines.append(f"  --color-success-surface: {success}11;")

        # warning 계열
        lines.append(f"  --color-warning-bg: {warning}15;")
        lines.append(f"  --color-warning-subtle: {warning}22;")
        lines.append(f"  --color-warning-border: {warning}44;")
        lines.append(f"  --color-warning-surface: {warning}11;")

        # error 계열
        lines.append(f"  --color-error-subtle: {error}22;")
        lines.append(f"  --color-error-border: {error}44;")
        lines.append(f"  --color-error-surface: {error}11;")
        lines.append(f"  --color-error-ring: {error}33;")
        lines.append(f"  --color-error-rgb: {_hex_to_rgb(error)};")

        # info 계열
        lines.append(f"  --color-info-subtle: {info}22;")
        lines.append(f"  --color-info-border: {info}44;")
        lines.append(f"  --color-info-surface: {info}11;")

        # --- 타이포그래피 ---
        typo = self.typography
        font_family = typo.get("font_family", "Pretendard")
        ff_css = f"'{font_family}', -apple-system, BlinkMacSystemFont, sans-serif"
        lines.append(f"  --font-family: {ff_css};")
        lines.append(f"  --font-family-base: {ff_css};")

        body_size = typo.get("body_size", "16px")
        line_height = typo.get("line_height", 1.6)
        lines.append(f"  --font-size-body: {body_size};")
        lines.append(f"  --line-height: {line_height};")

        for i, scale in enumerate(typo.get("heading_scale", []), 1):
            lines.append(f"  --font-size-h{i}: {scale}rem;")

        # 폰트 크기 스케일 (컴포넌트가 참조)
        lines.append("  --font-xs: 0.75rem;")
        lines.append("  --font-sm: 0.875rem;")
        lines.append(f"  --font-base: {body_size};")
        lines.append("  --font-md: 1.125rem;")
        lines.append("  --font-lg: 1.25rem;")
        lines.append("  --font-xl: 1.5rem;")
        lines.append("  --font-2xl: 2rem;")

        # 폰트 가중치
        weights = typo.get("weights", {})
        lines.append(f"  --font-weight-medium: {weights.get('medium', 500)};")
        lines.append(f"  --font-weight-bold: {weights.get('bold', 700)};")

        # 글자 간격
        ls_heading = typo.get("letter_spacing_heading", "-0.02em")
        ls_caption = typo.get("letter_spacing_caption", "0.04em")
        lines.append(f"  --ls-uppercase: {ls_caption};")

        # --- 간격 (기본 + 숫자 별칭) ---
        sp = self.spacing
        for name, value in sp.items():
            lines.append(f"  --space-{name}: {value};")

        # 숫자 별칭 매핑
        sp_order = ["xs", "sm", "md", "lg", "xl", "2xl", "3xl"]
        for i, k in enumerate(sp_order, 1):
            if k in sp:
                lines.append(f"  --space-{i}: {sp[k]};")
                lines.append(f"  --spacing-{k}: {sp[k]};")

        # --- 브레이크포인트 ---
        for name, value in self.breakpoints.items():
            lines.append(f"  --bp-{name}: {value};")

        # --- 이펙트 (기본 + 시맨틱 별칭) ---
        eff = self.effects
        for name, value in eff.items():
            css_name = name.replace("_", "-")
            lines.append(f"  --{css_name}: {value};")

        # 시맨틱 별칭
        lines.append(f"  --radius-card: {eff.get('radius_lg', '14px')};")
        lines.append(f"  --shadow-card-hover: {eff.get('shadow_lg', '0 10px 25px rgba(0,0,0,0.15)')};")
        lines.append(f"  --ease-out: ease-out;")
        lines.append(f"  --motion-fast: {eff.get('transition_fast', '.15s ease-out')};")
        lines.append(f"  --motion-normal: {eff.get('transition_normal', '.25s ease-out')};")
        lines.append(f"  --transition-base: {eff.get('transition_normal', '.25s ease-out')};")
        lines.append(f"  --focus-ring: 0 0 0 3px {primary}44;")

        lines.append("}")
        return "\n".join(lines)

    def content_hash(self) -> str:
        """토큰 내용의 해시 (변경 감지용)."""
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 2. 컴포넌트
# ---------------------------------------------------------------------------

@dataclass
class Component:
    """재사용 가능한 UI 컴포넌트.

    HTML 템플릿 + 컴포넌트 로컬 CSS.
    슬롯({{slot_name}})으로 데이터 주입 지점을 표시.
    """
    id: str = ""
    name: str = ""            # 예: "hero_header", "data_table", "stat_card"
    category: str = ""        # 예: "layout", "data", "navigation", "content"
    description: str = ""

    # HTML 템플릿 (Mustache 슬롯)
    # 예: '<div class="card"><h3>{{title}}</h3><p>{{body}}</p></div>'
    html_template: str = ""

    # 컴포넌트 로컬 CSS (토큰 변수 참조)
    # 예: '.card { background: var(--color-surface); border-radius: var(--radius-md); }'
    css: str = ""

    # 슬롯 정의 (이름 → 타입/설명)
    slots: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # 예: {"title": {"type": "text", "required": true}, "items": {"type": "list"}}

    # 반응형 CSS 오버라이드 (브레이크포인트별)
    responsive_css: Dict[str, str] = field(default_factory=dict)
    # 예: {"mobile": ".card { padding: var(--space-sm); }"}

    # 중첩 가능한 자식 컴포넌트 목록
    children_allowed: List[str] = field(default_factory=list)

    version: int = 1


# ---------------------------------------------------------------------------
# 3. 페이지 레시피
# ---------------------------------------------------------------------------

@dataclass
class SlotBinding:
    """레시피에서 컴포넌트 슬롯에 값을 바인딩하는 단위."""
    slot_name: str = ""
    value: Any = None              # 정적 값 (문자열, 리스트 등)
    source_artifact: str = ""      # 다른 산출물에서 가져올 경우 artifact_id
    source_path: str = ""          # 산출물 내 JSON path (예: "sections[0].content")
    transform: str = ""            # 선택적 변환 ("markdown_to_html", "truncate:200" 등)


@dataclass
class ComponentPlacement:
    """레시피 내 컴포넌트 배치 지시."""
    component_name: str = ""       # ComponentLibrary 내 컴포넌트 이름
    order: int = 0                 # 페이지 내 순서
    bindings: List[SlotBinding] = field(default_factory=list)
    condition: str = ""            # 조건부 렌더링 ("if data.items", "unless empty" 등)
    repeat: str = ""               # 반복 렌더링 소스 ("data.items" → 리스트 순회)
    wrapper_css_class: str = ""    # 추가 래퍼 클래스


@dataclass
class PageRecipe:
    """페이지 조립 레시피 — 컴포넌트 배치 + 데이터 바인딩 지시서.

    AI가 이것만 생성하면, renderer가 토큰+라이브러리+레시피로 완성 HTML을 조립.
    """
    id: str = ""
    project_id: str = ""
    page_name: str = ""            # 예: "메인 대시보드", "프로젝트 상세"
    page_slug: str = ""            # URL-safe 이름 (예: "main-dashboard")

    # 페이지 메타
    title: str = ""
    description: str = ""
    layout: str = "single-column"  # "single-column", "sidebar-left", "two-column", "grid"

    # 컴포넌트 배치 목록 (order 순 렌더링)
    placements: List[ComponentPlacement] = field(default_factory=list)

    # 페이지 전용 CSS (토큰 + 컴포넌트 위에 추가)
    page_css: str = ""

    # 페이지 전용 JS (인터랙션)
    page_js: str = ""

    version: int = 1


# ---------------------------------------------------------------------------
# 3-1. 필수 UX Placement 정의
# ---------------------------------------------------------------------------

REQUIRED_UX_PLACEMENTS: list[dict] = [
    {
        "component_name": "loading_indicator",
        "order": -100,
        "condition": "if _page_loading",
        "description": "페이지 로딩 스피너/스켈레톤",
    },
    {
        "component_name": "error_boundary",
        "order": -90,
        "condition": "if _page_error",
        "description": "에러 바운더리 (폴백 UI)",
    },
    {
        "component_name": "empty_state",
        "order": -80,
        "condition": "if _data_empty",
        "description": "데이터 없음 상태 표시",
    },
    {
        "component_name": "toast_container",
        "order": 9000,
        "condition": "",
        "description": "토스트 알림 컨테이너 (페이지 하단 고정)",
    },
    {
        "component_name": "modal_container",
        "order": 9100,
        "condition": "",
        "description": "모달/다이얼로그 컨테이너 (포탈 루트)",
    },
]


def ensure_required_placements(recipe: PageRecipe) -> PageRecipe:
    """레시피에 필수 UX placement가 없으면 자동 추가.

    이미 같은 component_name이 있으면 스킵 (중복 방지).
    원본을 변경하지 않고 새 PageRecipe를 반환.
    """
    existing_names = {p.component_name for p in recipe.placements}
    new_placements = list(recipe.placements)

    for req in REQUIRED_UX_PLACEMENTS:
        if req["component_name"] not in existing_names:
            new_placements.append(ComponentPlacement(
                component_name=req["component_name"],
                order=req["order"],
                condition=req.get("condition", ""),
            ))

    if len(new_placements) == len(recipe.placements):
        return recipe  # 변경 없음

    return PageRecipe(
        id=recipe.id,
        project_id=recipe.project_id,
        page_name=recipe.page_name,
        page_slug=recipe.page_slug,
        title=recipe.title,
        description=recipe.description,
        layout=recipe.layout,
        placements=new_placements,
        page_css=recipe.page_css,
        page_js=recipe.page_js,
        version=recipe.version,
    )


# ---------------------------------------------------------------------------
# 4. 레지스트리 (DB 연동)
# ---------------------------------------------------------------------------

class CompositionRegistry:
    """토큰·컴포넌트·레시피를 DB에 저장/로드/검색하는 레지스트리.

    DB 테이블: composition_tokens, composition_components, composition_recipes
    (마이그레이션은 Phase 7에서 추가)
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ── 토큰 ──

    async def save_tokens(self, tokens: DesignTokens) -> str:
        """디자인 토큰 저장/업데이트. 반환: token ID."""
        now = _now()
        if not tokens.id:
            tokens.id = str(uuid.uuid4())

        data = json.dumps(asdict(tokens), ensure_ascii=False)
        content_hash = tokens.content_hash()

        existing = await self.db.fetchone(
            "SELECT id, version FROM composition_tokens WHERE project_id=?",
            (tokens.project_id,),
        )

        if existing:
            tokens.id = existing["id"]
            tokens.version = (existing["version"] or 0) + 1
            await self.db.execute(
                "UPDATE composition_tokens SET data=?, content_hash=?, "
                "version=?, updated_at=? WHERE id=?",
                (data, content_hash, tokens.version, now, tokens.id),
            )
        else:
            await self.db.execute(
                "INSERT INTO composition_tokens "
                "(id, project_id, data, content_hash, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tokens.id, tokens.project_id, data, content_hash,
                 tokens.version, now, now),
            )

        logger.info("tokens_saved project=%s version=%d hash=%s",
                     tokens.project_id, tokens.version, content_hash)
        return tokens.id

    async def load_tokens(self, project_id: str) -> Optional[DesignTokens]:
        """프로젝트의 현재 디자인 토큰 로드."""
        row = await self.db.fetchone(
            "SELECT data, version FROM composition_tokens WHERE project_id=?",
            (project_id,),
        )
        if not row:
            return None
        tokens = _dict_to_tokens(json.loads(row["data"]))
        tokens.version = row["version"]  # DB version이 정본
        return tokens

    # ── 컴포넌트 ──

    async def save_component(self, project_id: str, comp: Component) -> str:
        """컴포넌트 저장/업데이트. 반환: component ID."""
        now = _now()
        if not comp.id:
            comp.id = str(uuid.uuid4())

        data = json.dumps(asdict(comp), ensure_ascii=False)

        existing = await self.db.fetchone(
            "SELECT id, version FROM composition_components "
            "WHERE project_id=? AND name=?",
            (project_id, comp.name),
        )

        if existing:
            comp.id = existing["id"]
            comp.version = (existing["version"] or 0) + 1
            await self.db.execute(
                "UPDATE composition_components SET data=?, version=?, updated_at=? "
                "WHERE id=?",
                (data, comp.version, now, comp.id),
            )
        else:
            await self.db.execute(
                "INSERT INTO composition_components "
                "(id, project_id, name, category, data, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (comp.id, project_id, comp.name, comp.category,
                 data, comp.version, now, now),
            )

        logger.info("component_saved project=%s name=%s version=%d",
                     project_id, comp.name, comp.version)
        return comp.id

    async def save_components_bulk(self, project_id: str, components: List[Component]) -> int:
        """여러 컴포넌트 일괄 저장. 반환: 저장 건수."""
        count = 0
        for comp in components:
            await self.save_component(project_id, comp)
            count += 1
        return count

    async def save_components_bulk_upsert(self, project_id: str, components: List[Component]) -> int:
        """컴포넌트 일괄 저장/업데이트 (N+1 최적화). 반환: 저장 건수.

        하나의 ON CONFLICT 쿼리로 모든 컴포넌트를 atomic하게 처리.
        기존 컴포넌트는 version 증가 + updated_at 갱신, 신규는 새로 삽입.
        """
        if not components:
            return 0

        now = _now()
        rows = []
        for comp in components:
            if not comp.id:
                comp.id = str(uuid.uuid4())

            data = json.dumps(asdict(comp), ensure_ascii=False)
            rows.append((
                comp.id,
                project_id,
                comp.name,
                comp.category or "",
                data,
                comp.version,
                now,
                now,
            ))

        await self.db.executemany(
            """INSERT INTO composition_components
               (id, project_id, name, category, data, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, name) DO UPDATE SET
                 id=excluded.id,
                 data=excluded.data,
                 version=COALESCE(version, 0) + 1,
                 updated_at=excluded.updated_at""",
            rows,
        )

        logger.info("components_bulk_upsert project=%s count=%d", project_id, len(components))
        return len(components)

    async def load_component(self, project_id: str, name: str) -> Optional[Component]:
        """이름으로 컴포넌트 로드."""
        row = await self.db.fetchone(
            "SELECT data FROM composition_components "
            "WHERE project_id=? AND name=?",
            (project_id, name),
        )
        if not row:
            return None
        return _dict_to_component(json.loads(row["data"]))

    async def load_all_components(self, project_id: str) -> List[Component]:
        """프로젝트의 모든 컴포넌트 로드."""
        rows = await self.db.fetchall(
            "SELECT data FROM composition_components WHERE project_id=? "
            "ORDER BY category, name",
            (project_id,),
        )
        return [_dict_to_component(json.loads(r["data"])) for r in rows]

    async def load_components_by_category(
        self, project_id: str, category: str
    ) -> List[Component]:
        """카테고리별 컴포넌트 로드."""
        rows = await self.db.fetchall(
            "SELECT data FROM composition_components "
            "WHERE project_id=? AND category=?",
            (project_id, category),
        )
        return [_dict_to_component(json.loads(r["data"])) for r in rows]

    # ── 레시피 ──

    async def save_recipe(self, recipe: PageRecipe) -> str:
        """페이지 레시피 저장/업데이트. 반환: recipe ID."""
        now = _now()
        if not recipe.id:
            recipe.id = str(uuid.uuid4())

        data = json.dumps(_recipe_to_dict(recipe), ensure_ascii=False)

        existing = await self.db.fetchone(
            "SELECT id, version FROM composition_recipes "
            "WHERE project_id=? AND page_slug=?",
            (recipe.project_id, recipe.page_slug),
        )

        if existing:
            recipe.id = existing["id"]
            recipe.version = (existing["version"] or 0) + 1
            await self.db.execute(
                "UPDATE composition_recipes SET data=?, page_name=?, "
                "version=?, updated_at=? WHERE id=?",
                (data, recipe.page_name, recipe.version, now, recipe.id),
            )
        else:
            await self.db.execute(
                "INSERT INTO composition_recipes "
                "(id, project_id, page_slug, page_name, data, version, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (recipe.id, recipe.project_id, recipe.page_slug,
                 recipe.page_name, data, recipe.version, now, now),
            )

        logger.info("recipe_saved project=%s page=%s version=%d",
                     recipe.project_id, recipe.page_name, recipe.version)
        return recipe.id

    async def load_recipe(self, project_id: str, page_slug: str) -> Optional[PageRecipe]:
        """페이지 슬러그로 레시피 로드."""
        row = await self.db.fetchone(
            "SELECT data FROM composition_recipes "
            "WHERE project_id=? AND page_slug=?",
            (project_id, page_slug),
        )
        if not row:
            return None
        return _dict_to_recipe(json.loads(row["data"]))

    async def load_all_recipes(self, project_id: str) -> List[PageRecipe]:
        """프로젝트의 모든 레시피 로드."""
        rows = await self.db.fetchall(
            "SELECT data FROM composition_recipes WHERE project_id=? "
            "ORDER BY page_name",
            (project_id,),
        )
        return [_dict_to_recipe(json.loads(r["data"])) for r in rows]

    # ── 변경 감지 ──

    async def tokens_changed(self, project_id: str, new_hash: str) -> bool:
        """토큰 해시 비교로 변경 여부 확인."""
        row = await self.db.fetchone(
            "SELECT content_hash FROM composition_tokens WHERE project_id=?",
            (project_id,),
        )
        if not row:
            return True  # 토큰 없음 = 변경된 것으로 취급
        return row["content_hash"] != new_hash

    async def get_component_names(self, project_id: str) -> List[str]:
        """프로젝트에 등록된 컴포넌트 이름 목록."""
        rows = await self.db.fetchall(
            "SELECT name FROM composition_components WHERE project_id=? ORDER BY name",
            (project_id,),
        )
        return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# 직렬화 헬퍼 (dataclass ↔ dict)
# ---------------------------------------------------------------------------

def _dict_to_tokens(d: Dict) -> DesignTokens:
    return DesignTokens(
        id=d.get("id", ""),
        project_id=d.get("project_id", ""),
        colors=d.get("colors", {}),
        typography=d.get("typography", {}),
        spacing=d.get("spacing", {}),
        breakpoints=d.get("breakpoints", {}),
        effects=d.get("effects", {}),
        meta=d.get("meta", {}),
        version=d.get("version", 1),
    )


def _dict_to_component(d: Dict) -> Component:
    return Component(
        id=d.get("id", ""),
        name=d.get("name", ""),
        category=d.get("category", ""),
        description=d.get("description", ""),
        html_template=d.get("html_template", ""),
        css=d.get("css", ""),
        slots=d.get("slots", {}),
        responsive_css=d.get("responsive_css", {}),
        children_allowed=d.get("children_allowed", []),
        version=d.get("version", 1),
    )


def _recipe_to_dict(recipe: PageRecipe) -> Dict:
    """PageRecipe → JSON-safe dict (중첩 dataclass 처리)."""
    d = asdict(recipe)
    # asdict가 SlotBinding, ComponentPlacement도 자동 변환
    return d


def _dict_to_recipe(d: Dict) -> PageRecipe:
    placements = []
    for p in d.get("placements", []):
        bindings = []
        for b in p.get("bindings", []):
            bindings.append(SlotBinding(
                slot_name=b.get("slot_name", ""),
                value=b.get("value"),
                source_artifact=b.get("source_artifact", ""),
                source_path=b.get("source_path", ""),
                transform=b.get("transform", ""),
            ))
        placements.append(ComponentPlacement(
            component_name=p.get("component_name", ""),
            order=p.get("order", 0),
            bindings=bindings,
            condition=p.get("condition", ""),
            repeat=p.get("repeat", ""),
            wrapper_css_class=p.get("wrapper_css_class", ""),
        ))

    return PageRecipe(
        id=d.get("id", ""),
        project_id=d.get("project_id", ""),
        page_name=d.get("page_name", ""),
        page_slug=d.get("page_slug", ""),
        title=d.get("title", ""),
        description=d.get("description", ""),
        layout=d.get("layout", "single-column"),
        placements=placements,
        page_css=d.get("page_css", ""),
        page_js=d.get("page_js", ""),
        version=d.get("version", 1),
    )


def _now() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
