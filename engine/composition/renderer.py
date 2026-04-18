"""
engine/composition/renderer.py
컴포넌트 조합 렌더러 — AI 호출 0회로 완성 HTML 페이지 조립.

입력: DesignTokens + ComponentLibrary + PageRecipe
출력: 완성된 HTML 문자열 (반응형, 토큰 기반 스타일링)

핵심 원칙:
  - 순수 Python 문자열 조립 (외부 템플릿 엔진 불필요)
  - 토큰 변경 → CSS 변수만 바뀜 → 전체 재조립 (AI 0회)
  - 컴포넌트 변경 → 해당 컴포넌트 사용 페이지만 재조립 (AI 0회)
  - 레시피 변경 → 해당 페이지만 재조립 (AI 0회)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from engine.composition.registry import (
    Component,
    ComponentPlacement,
    CompositionRegistry,
    DesignTokens,
    PageRecipe,
    SlotBinding,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 렌더 결과
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    """렌더링 결과."""
    html: str = ""
    page_name: str = ""
    page_slug: str = ""
    content_hash: str = ""
    components_used: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 메인 렌더러
# ---------------------------------------------------------------------------

class CompositionRenderer:
    """토큰 + 컴포넌트 + 레시피 → 완성 HTML.

    사용법::

        renderer = CompositionRenderer(registry)
        result = await renderer.render_page(project_id, "main-dashboard")
        # result.html → 완성된 HTML 문자열
    """

    def __init__(self, registry: CompositionRegistry) -> None:
        self.registry = registry

    async def render_page(
        self,
        project_id: str,
        page_slug: str,
        data_context: Optional[Dict[str, Any]] = None,
        preview_mode: bool = False,
    ) -> RenderResult:
        """단일 페이지 렌더링.

        Args:
            project_id:   프로젝트 UUID.
            page_slug:    렌더링할 페이지 슬러그.
            data_context: 슬롯 바인딩에 사용할 런타임 데이터 (선택).

        Returns:
            RenderResult with 완성 HTML + 메타데이터.
        """
        result = RenderResult(page_slug=page_slug)
        data_ctx = data_context or {}

        # 1. 로드: 토큰, 레시피, 필요한 컴포넌트
        tokens = await self.registry.load_tokens(project_id)
        recipe = await self.registry.load_recipe(project_id, page_slug)

        if not recipe:
            result.warnings.append(f"레시피 없음: {page_slug}")
            result.html = _error_page(f"페이지 레시피를 찾을 수 없습니다: {page_slug}")
            return result

        result.page_name = recipe.page_name

        # 필요한 컴포넌트만 로드
        needed_names = {p.component_name for p in recipe.placements}
        components: Dict[str, Component] = {}
        for name in needed_names:
            comp = await self.registry.load_component(project_id, name)
            if comp:
                components[name] = comp
            else:
                result.warnings.append(f"컴포넌트 없음: {name}")

        result.components_used = sorted(needed_names)

        # 2. 조립
        css_parts = []
        body_parts = []

        # 2-1. 토큰 → CSS 변수
        if tokens:
            css_parts.append(tokens.to_css_variables())
            css_parts.append(_base_reset_css())
        else:
            result.warnings.append("디자인 토큰 없음 — 기본 스타일 사용")
            css_parts.append(_fallback_tokens_css())
            css_parts.append(_base_reset_css())

        # 2-2. 컴포넌트 CSS 수집
        for name in sorted(components):
            comp = components[name]
            if comp.css:
                css_parts.append(f"/* component: {name} */")
                css_parts.append(comp.css)
            # 반응형 CSS
            for bp_name, bp_css in comp.responsive_css.items():
                bp_var = f"var(--bp-{bp_name})"
                # 토큰에서 실제 값 가져오기
                bp_value = ""
                if tokens and bp_name in tokens.breakpoints:
                    bp_value = tokens.breakpoints[bp_name]
                elif bp_name == "mobile":
                    bp_value = "480px"
                elif bp_name == "tablet":
                    bp_value = "768px"
                else:
                    bp_value = "1024px"
                css_parts.append(f"@media (max-width: {bp_value}) {{")
                css_parts.append(f"  {bp_css}")
                css_parts.append("}")

        # 2-3. 페이지 전용 CSS
        if recipe.page_css:
            css_parts.append(f"/* page: {recipe.page_slug} */")
            css_parts.append(recipe.page_css)

        # 2-4. 레이아웃 CSS
        css_parts.append(_layout_css(recipe.layout))

        # 2-5. 컴포넌트 배치 → HTML 조립
        sorted_placements = sorted(recipe.placements, key=lambda p: p.order)

        for placement in sorted_placements:
            comp = components.get(placement.component_name)
            if not comp:
                body_parts.append(
                    f'<!-- missing component: {placement.component_name} -->'
                )
                continue

            # 조건부 렌더링
            if placement.condition and not _evaluate_condition(
                placement.condition, data_ctx
            ):
                continue

            # 반복 렌더링
            if placement.repeat:
                items = _resolve_path(placement.repeat, data_ctx)
                if isinstance(items, list):
                    for i, item in enumerate(items):
                        item_ctx = {**data_ctx, "_item": item, "_index": i}
                        rendered = _render_component(comp, placement, item_ctx, preview_mode)
                        body_parts.append(
                            _wrap_placement(rendered, placement, f"repeat-{i}")
                        )
                    continue

            # 단일 렌더링
            rendered = _render_component(comp, placement, data_ctx, preview_mode)
            body_parts.append(_wrap_placement(rendered, placement))

        # 2-6. 미리보기 모드: 라이트 테마 + 잔여 핸들바 태그 정리
        if preview_mode:
            css_parts.append(_preview_light_theme_css())

        # 3. 최종 HTML 조립
        full_css = "\n".join(css_parts)
        full_body = "\n".join(body_parts)
        if preview_mode:
            # 잔여 핸들바 태그 정리 (렌더링 안 된 {{...}})
            full_body = re.sub(r"\{\{[^}]+\}\}", "", full_body)
        page_js = recipe.page_js or ""

        html = _assemble_html(
            title=recipe.title or recipe.page_name,
            description=recipe.description,
            css=full_css,
            body=full_body,
            layout=recipe.layout,
            js=page_js,
            meta=tokens.meta if tokens else {},
        )

        result.html = html
        result.content_hash = hashlib.sha256(html.encode()).hexdigest()[:16]

        logger.info(
            "page_rendered project=%s page=%s components=%d warnings=%d hash=%s",
            project_id, page_slug, len(components), len(result.warnings),
            result.content_hash,
        )
        return result

    async def render_all_pages(
        self,
        project_id: str,
        data_context: Optional[Dict[str, Any]] = None,
        preview_mode: bool = False,
    ) -> List[RenderResult]:
        """프로젝트의 모든 페이지 렌더링."""
        recipes = await self.registry.load_all_recipes(project_id)
        results = []
        for recipe in recipes:
            result = await self.render_page(
                project_id, recipe.page_slug, data_context, preview_mode
            )
            results.append(result)
        return results

    async def render_preview(
        self,
        project_id: str,
        component_name: str,
        sample_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """단일 컴포넌트 미리보기 HTML 생성 (개발/디버깅용)."""
        tokens = await self.registry.load_tokens(project_id)
        comp = await self.registry.load_component(project_id, component_name)
        if not comp:
            return _error_page(f"컴포넌트 없음: {component_name}")

        css = ""
        if tokens:
            css = tokens.to_css_variables() + "\n" + _base_reset_css()
        else:
            css = _fallback_tokens_css() + "\n" + _base_reset_css()

        if comp.css:
            css += f"\n{comp.css}"

        html_body = _render_slots(comp.html_template, sample_data or {})

        return _assemble_html(
            title=f"Preview: {component_name}",
            description="",
            css=css,
            body=f'<div class="preview-container">{html_body}</div>',
            layout="single-column",
            js="",
            meta={},
        )

    async def diff_impact(
        self, project_id: str, changed: str
    ) -> List[str]:
        """변경된 요소가 영향을 미치는 페이지 슬러그 목록.

        Args:
            changed: "tokens" | 컴포넌트 이름

        Returns:
            재조립이 필요한 페이지 슬러그 목록.
        """
        recipes = await self.registry.load_all_recipes(project_id)

        if changed == "tokens":
            # 토큰 변경 → 모든 페이지 영향
            return [r.page_slug for r in recipes]

        # 특정 컴포넌트 변경 → 해당 컴포넌트를 사용하는 페이지만
        affected = []
        for recipe in recipes:
            for p in recipe.placements:
                if p.component_name == changed:
                    affected.append(recipe.page_slug)
                    break
        return affected


# ---------------------------------------------------------------------------
# 미리보기 자동 데이터 생성
# ---------------------------------------------------------------------------

# 슬롯 이름 → 더미 값 매핑 (범용)
_PREVIEW_SLOT_DEFAULTS: Dict[str, Any] = {
    # 텍스트
    "title": "제목입니다", "subtitle": "부제목입니다", "name": "홍길동",
    "description": "설명 텍스트입니다.", "label": "항목", "text": "텍스트",
    "placeholder": "검색어를 입력하세요", "message": "알림 메시지입니다.",
    "heading": "헤딩", "content": "콘텐츠 내용입니다.",
    "logo_text": "로고", "brand": "명성실버케어", "brand_name": "명성케어",
    "brand_desc": "전문 요양 서비스",
    "footer_text": "© 2024 명성실버케어센터. All rights reserved.",
    "copyright": "© 2024 명성실버케어센터. All rights reserved.",
    "empty_message": "데이터가 없습니다.",
    "action_button_label": "새로 만들기", "action_button_href": "#",
    "action_button_color": "#4f46e5",
    "body": "내용이 여기에 표시됩니다.",
    "author": "관리자", "memo": "메모 내용입니다.",
    "summary": "요약 내용입니다.",
    # 아이콘/UI 요소
    "search_icon": "🔍", "clear_icon": "✕", "prev_icon": "‹", "next_icon": "›",
    "node_icon": "●", "client_icon": "👤", "distance_icon": "📍",
    "icon": "📋", "check": "✓", "close_icon": "✕",
    "hamburger_icon": "☰",
    "logo": '<span style="font-weight:700;font-size:1.2em;">명성케어</span>',
    "nhis_icon": "🏛️",
    # 네비게이션/액션
    "user_menu": "내 정보", "cta_label": "시작하기", "cta_href": "#",
    "user_profile": "관리자",
    "submit_button": "제출", "edit_button": "수정",
    "action_label": "실행", "nav_button": "이동",
    "toolbar_actions": "", "action_buttons": "",
    # 통계/수치
    "count": 42, "total": 128, "amount": "1,250,000",
    "current_page": 1, "total_pages": 5, "per_page": 10,
    "stat_value": "127", "stat_label": "총 건수", "stat_change": "+3.2%",
    "age": 78, "score": 4.5, "rating": 4, "price": "50,000",
    "percentage": "78%", "avg_rating": 4.5, "total_reviews": 89,
    "total_text": "총 128건", "total_amount": "3,750,000원",
    "total_distance": "12.5km", "total_time": "2시간 30분",
    "value": "127", "unit": "명", "change": "+3.2%", "change_label": "전월 대비",
    "num": 3, "sparkline": "",
    "score_label": "4.5 / 5.0",
    "visit_count": 3, "checked_count": 2, "total_count": 5,
    "unassigned_count": 2,
    "expected_amount": "4,200,000원",
    "approved_count": 45, "pending_count": 3, "rejected_count": 1,
    "total_count": 48, "deadline_date": "2024-01-25",
    "completed_count": 5, "pending_journals": 2,
    # 보고서 레이블
    "month": "2024년 1월",
    # 색상/스타일
    "bg_color": "#f8f9fb", "text_color": "#1a1a2e",
    "active_color": "#4f46e5", "color": "#4f46e5",
    # URL
    "href": "#", "url": "#", "src": "", "photo_url": "", "photo": "",
    "icon_url": "", "image_url": "", "link_href": "#",
    "action_href": "#", "journal_href": "#",
    # 상태
    "status": "활성", "state": "진행중", "type": "info", "category": "공지",
    "variant": "", "size": "",
    "grade": "A", "level": "1", "priority": "보통",
    "sync_status": "동기화 완료", "last_sync": "2024-01-15 09:00",
    "status_label": "정상", "status_badge": "활성",
    "conn_label": "연결됨", "period_badge": "진행중",
    "grade_label": "1등급", "error_message": "",
    "nhis_status": "연동 완료", "nhis_status_type": "success",
    "nhis_status_label": "정상",
    "period": "2024년 1월",
    "rejected_list": "", "legend": "",
    # 날짜/시간
    "date": "2024-01-15", "time": "09:00", "created_at": "2024-01-15",
    "updated_at": "2024-01-15", "start_date": "2024-01-01", "end_date": "2024-12-31",
    "last_sync_at": "2024-01-15 09:00", "visit_date": "2024-01-15",
    "duration": "2시간", "time_range": "09:00~11:00",
    "start_time": "09:00", "end_time": "11:00",
    "plan_start": "2024-01-01", "plan_end": "2024-12-31",
    "week_label": "2024년 1월 3주차",
    "last_journal_date": "2024-01-14",
    "last_journal_preview": "상태 양호, 식사 잘 하심",
    "checkin_time": "09:05", "checkout_time": "11:02",
    # 캘린더 — year, month 를 숫자로 제공
    "year": 2024, "month": 1,
    # 불리언
    "active": True, "selected": False, "loading": False,
    "selectable": False, "sortable": True, "pinned": False,
    "is_online": True, "is_recording": False,
    "show_mobile_nav": False, "show_footer": True,
    "is_logged_in": False,
    # 개인정보
    "phone": "010-1234-5678", "email": "user@example.com",
    "address": "서울시 강남구 역삼동 123-45",
    "current_location": "서울시 강남구 역삼동 123-45",
    "visiting_client": "홍길동",
    "id": "001", "key": "item-1",
    "field_id": "field-1", "field_name": "field_name",
    "client_name": "홍길동", "caregiver_name": "김요양",
    "guardian_name": "홍보호", "guardian_relation": "자녀",
    "guardian_phone": "010-9876-5432", "guardian_phone_raw": "01098765432",
    "caregiver_phone": "010-1111-2222", "caregiver_phone_raw": "01011112222",
    "caregiver_avatar": "👩‍⚕️",
    "gender": "여", "care_grade": "장기요양 3등급", "care_level": "3",
    "service_types": "방문요양, 방문목욕",
    "special_notes": "특이사항 없음",
    "weekly_visits": "3회", "visit_hours": "6시간",
    "bp": "120/80", "bp_systolic": "120", "bp_diastolic": "80",
    "temp": "36.5", "temperature": "36.5",
    "pulse": "72", "spo2": "98",
    "distance": "3.2km",
    "tag_buttons": "",
    "phone_icon": "📞",
    "state_icon": "🟢",
    "sync_count": 0,
    "hint_text": "버튼을 눌러 음성으로 입력하세요",
    # separator
    "separator": "›",
    # 차트/임베드
    "chart_svg": '<div style="height:200px;background:linear-gradient(135deg,#f0f4f8,#e2e8f0);border-radius:12px;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:14px;">📊 차트 영역</div>',
    "map_embed": '<div style="height:300px;background:linear-gradient(135deg,#e8f4f8,#d1ecf1);border-radius:12px;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:14px;">🗺️ 지도 영역</div>',
    "map_content": '<div style="height:250px;background:linear-gradient(135deg,#e8f4f8,#d1ecf1);border-radius:12px;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:14px;">🗺️ 실시간 위치</div>',
    "service_tags": "방문요양",
    "illustration": "",
    "media": "",
    "options": "",
    "bg_image": "",
    "eyebrow": "",
    "number": "01",
    "photo_url": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='72' height='72'%3E%3Crect fill='%23e2e8f0' width='72' height='72' rx='36'/%3E%3Ctext x='36' y='44' text-anchor='middle' fill='%2394a3b8' font-size='28'%3E👤%3C/text%3E%3C/svg%3E",
}

# {{#each X}} 블록 내 _item 필드용 더미 행 생성
_PREVIEW_ROW_TEMPLATES: Dict[str, list] = {
    # 테이블 행 — 범용 (컬럼 수에 맞는 cells 포함)
    "rows": [
        {"id": "001", "name": "김영수 어르신", "status": "활성", "date": "2024-01-15", "value": "850,000", "category": "방문요양", "author": "김요양", "views": 156,
         "cells": ["001", "공지", "1월 운영 일정 안내", "전체", "관리자", "2024-01-15", "156", "Y", "보기"]},
        {"id": "002", "name": "박미경 어르신", "status": "대기", "date": "2024-01-14", "value": "720,000", "category": "방문목욕", "author": "이돌봄", "views": 89,
         "cells": ["002", "안내", "요양보호사 교육 안내", "요양보호사", "담당자", "2024-01-14", "89", "N", "보기"]},
        {"id": "003", "name": "이순자 어르신", "status": "완료", "date": "2024-01-13", "value": "1,100,000", "category": "방문요양", "author": "박케어", "views": 234,
         "cells": ["003", "긴급", "코로나19 방역 지침 변경", "전체", "관리자", "2024-01-13", "234", "Y", "보기"]},
    ],
    # 네비게이션
    "menu_items": [
        {"label": "대시보드", "href": "#", "icon": "📊", "active": True},
        {"label": "관리", "href": "#", "icon": "⚙️", "active": False},
        {"label": "보고서", "href": "#", "icon": "📈", "active": False},
        {"label": "설정", "href": "#", "icon": "🔧", "active": False},
    ],
    "nav_items": [
        {"label": "홈", "href": "#", "active": True},
        {"label": "서비스", "href": "#", "active": False},
        {"label": "소개", "href": "#", "active": False},
        {"label": "문의", "href": "#", "active": False},
    ],
    # 필터/탭
    "filters": [
        {"label": "전체", "value": "all", "active": True},
        {"label": "진행중", "value": "active"},
        {"label": "완료", "value": "done"},
    ],
    "tabs": [
        {"id": "tab-1", "label": "월별 매출", "active": True, "panel_id": "panel-1"},
        {"id": "tab-2", "label": "요양보호사 정산", "panel_id": "panel-2"},
        {"id": "tab-3", "label": "고객 청구", "panel_id": "panel-3"},
    ],
    # 링크
    "links": [
        {"label": "이용약관", "href": "#"},
        {"label": "개인정보처리방침", "href": "#"},
        {"label": "고객센터", "href": "#"},
    ],
    # 서비스/태그
    "services": [
        {"name": "방문요양"},
        {"name": "방문목욕"},
        {"name": "주간보호"},
    ],
    "tags": [{"label": "방문요양"}, {"label": "3등급"}, {"label": "장기"}],
    # 사이드바 메뉴 그룹
    "menu_groups": [
        {"label": "메인", "items": [
            {"label": "대시보드", "href": "#", "icon": "📊", "active": True},
            {"label": "어르신 관리", "href": "#", "icon": "👥"},
            {"label": "일정/배정", "href": "#", "icon": "📅"},
            {"label": "요양보호사", "href": "#", "icon": "👩‍⚕️"},
        ]},
        {"label": "운영", "items": [
            {"label": "매출/정산", "href": "#", "icon": "💰"},
            {"label": "국민건강보험", "href": "#", "icon": "🏛️"},
            {"label": "모니터링", "href": "#", "icon": "📡"},
            {"label": "공지사항", "href": "#", "icon": "📢"},
            {"label": "설정", "href": "#", "icon": "⚙️"},
        ]},
    ],
    "sections": [
        {"title": "메인", "items": [
            {"label": "대시보드", "href": "#", "icon": "📊", "active": True},
            {"label": "어르신 관리", "href": "#", "icon": "👥"},
            {"label": "일정/배정", "href": "#", "icon": "📅"},
            {"label": "요양보호사", "href": "#", "icon": "👩‍⚕️"},
        ]},
        {"title": "운영", "items": [
            {"label": "매출/정산", "href": "#", "icon": "💰"},
            {"label": "국민건강보험", "href": "#", "icon": "🏛️"},
            {"label": "모니터링", "href": "#", "icon": "📡"},
            {"label": "공지사항", "href": "#", "icon": "📢"},
            {"label": "설정", "href": "#", "icon": "⚙️"},
        ]},
    ],
    # 컬럼 정의 — 페이지별로 다르지만 범용 9컬럼
    "columns": [
        {"key": "id", "label": "번호", "sortable": True},
        {"key": "category", "label": "분류"},
        {"key": "title", "label": "제목", "sortable": True},
        {"key": "target", "label": "대상"},
        {"key": "author", "label": "작성자"},
        {"key": "date", "label": "작성일", "sortable": True},
        {"key": "views", "label": "조회수", "sortable": True},
        {"key": "pinned", "label": "고정"},
        {"key": "action", "label": "관리"},
    ],
    # 통계
    "stats": [
        {"label": "이용 어르신", "value": "127", "change": "+5%", "icon": "👥", "status": "normal"},
        {"label": "요양보호사", "value": "42", "change": "+2%", "icon": "👩‍⚕️", "status": "normal"},
        {"label": "오늘 방문", "value": "38", "change": "-1건", "icon": "📋", "status": "warning"},
    ],
    # 페이지네이션
    "pages": [
        {"value": 1, "label": "1", "active": True},
        {"value": 2, "label": "2"},
        {"value": 3, "label": "3"},
    ],
    # 방문 일정
    "visits": [
        {"status": "completed", "start_time": "09:00", "end_time": "10:30", "client_name": "김영수",
         "address": "서울시 강남구 역삼동 123-45", "service_tags": "방문요양", "nav_button": "이동"},
        {"status": "active", "start_time": "11:00", "end_time": "12:30", "client_name": "박미경",
         "address": "서울시 서초구 반포동 456-7", "service_tags": "방문목욕", "nav_button": "이동"},
        {"status": "upcoming", "start_time": "14:00", "end_time": "15:30", "client_name": "이순자",
         "address": "서울시 강남구 청담동 789-1", "service_tags": "방문요양", "nav_button": "이동"},
    ],
    # 별점
    "stars": [
        {"value": 1, "label": "1점"},
        {"value": 2, "label": "2점"},
        {"value": 3, "label": "3점"},
        {"value": 4, "label": "4점"},
        {"value": 5, "label": "5점"},
    ],
    # KPI
    "kpis": [
        {"icon": "👥", "value": "127명", "label": "이용 어르신", "status": "normal", "action_href": "#", "action_label": "보기"},
        {"icon": "👩‍⚕️", "value": "42명", "label": "요양보호사", "status": "normal", "action_href": "#", "action_label": "보기"},
        {"icon": "🚨", "value": "0건", "label": "긴급 알림", "status": "success", "action_href": "#", "action_label": "보기"},
    ],
    # 배정 보드 — 주간 일정
    "days": [
        {"day": "월", "date": "2024-01-15",
         "caregiver_name": "김요양", "visit_count": 3,
         "client": "홍길동", "time_start": "09:00", "time_end": "11:00"},
        {"day": "화", "date": "2024-01-16",
         "caregiver_name": "이돌봄", "visit_count": 4,
         "client": "박미경", "time_start": "09:00", "time_end": "11:00"},
        {"day": "수", "date": "2024-01-17",
         "caregiver_name": "박케어", "visit_count": 2,
         "client": "이순자", "time_start": "10:00", "time_end": "12:00"},
    ],
    # 캘린더 요일 헤더
    "day_headers": [
        {"label": "일"}, {"label": "월"}, {"label": "화"},
        {"label": "수"}, {"label": "목"}, {"label": "금"}, {"label": "토"},
    ],
    # 캘린더 셀
    "calendar_days": [
        {"date": "2024-01-01", "num": 1, "type": "current"},
        {"date": "2024-01-02", "num": 2, "type": "current"},
        {"date": "2024-01-03", "num": 3, "type": "current", "is_selected": True, "dots": "●"},
        {"date": "2024-01-04", "num": 4, "type": "current"},
        {"date": "2024-01-05", "num": 5, "type": "current", "dots": "●"},
    ],
    # 보험 청구 항목
    "claims": [
        {"client": "김영수", "service": "방문요양", "amount": "850,000원", "status": "승인"},
        {"client": "박미경", "service": "방문목욕", "amount": "720,000원", "status": "심사중"},
        {"client": "이순자", "service": "방문요양", "amount": "1,100,000원", "status": "승인"},
    ],
    # 보험 청구 요약
    "breakdown": [
        {"color": "#52C78A", "value": "3,200,000", "label": "승인"},
        {"color": "#F5C842", "value": "720,000", "label": "심사중"},
        {"color": "#E05555", "value": "0", "label": "반려"},
    ],
    # 연락처
    "contacts": [
        {"label": "전화", "value": "02-1234-5678"},
        {"label": "팩스", "value": "02-1234-5679"},
        {"label": "이메일", "value": "info@myeongseong.kr"},
    ],
    # 푸터 링크 그룹
    "link_groups": [
        {"title": "서비스", "links": [
            {"label": "방문요양", "href": "#"},
            {"label": "방문목욕", "href": "#"},
            {"label": "주간보호", "href": "#"},
        ]},
        {"title": "회사 소개", "links": [
            {"label": "인사말", "href": "#"},
            {"label": "오시는 길", "href": "#"},
        ]},
        {"title": "고객지원", "links": [
            {"label": "자주 묻는 질문", "href": "#"},
            {"label": "상담 신청", "href": "#"},
        ]},
    ],
    # 소셜 링크
    "social_links": [
        {"href": "#", "label": "블로그"},
        {"href": "#", "label": "카카오톡"},
        {"href": "#", "label": "전화상담"},
    ],
    # 빵 부스러기 네비게이션
    "items": [
        {"label": "홈", "href": "#"},
        {"label": "어르신 관리", "href": "#"},
        {"label": "상세보기", "current": True},
    ],
    # 복약 관리
    "medications": [
        {"name": "혈압약", "dose": "1정", "schedule": "아침 식후", "note": ""},
        {"name": "당뇨약", "dose": "1정", "schedule": "아침·저녁 식후", "note": "공복 금지"},
    ],
    # 주간 요일
    "week_days": [
        {"label": "월", "status": "completed"},
        {"label": "화", "status": "completed"},
        {"label": "수", "status": "today"},
        {"label": "목", "status": "upcoming"},
        {"label": "금", "status": "upcoming"},
    ],
    # 근무 현황
    "metrics": [
        {"value": "24", "unit": "시간", "label": "근무 시간"},
        {"value": "12", "unit": "건", "label": "방문 완료"},
        {"value": "3", "unit": "건", "label": "잔여 방문"},
    ],
    # 하단 탭
    "bottom_tabs": [
        {"label": "홈", "href": "#", "icon": "🏠", "active": True},
        {"label": "일정", "href": "#", "icon": "📅"},
        {"label": "알림", "href": "#", "icon": "🔔", "badge": 2},
    ],
    # 챗봇
    "messages": [
        {"role": "bot", "text": "안녕하세요! 무엇을 도와드릴까요?", "time": "09:00"},
        {"role": "user", "text": "다음 방문 일정을 확인하고 싶어요", "time": "09:01"},
    ],
    # 빠른 액션
    "actions": [
        {"icon": "📋", "label": "돌봄 일지", "href": "#"},
        {"icon": "💊", "label": "복약 체크", "href": "#"},
        {"icon": "📞", "label": "센터 연락", "href": "#"},
        {"icon": "🚨", "label": "긴급 알림", "href": "#"},
    ],
    # 돌봄 사진
    "photos": [
        {"url": "", "alt": "돌봄 사진 1"},
        {"url": "", "alt": "돌봄 사진 2"},
    ],
    # 목표
    "goals": [
        {"text": "규칙적인 식사 관리"},
        {"text": "주 3회 가벼운 산책"},
        {"text": "복약 시간 준수"},
    ],
    # 태그 목록 (care_rating_form의 tag_buttons)
    "tag_options": [
        {"label": "친절해요"}, {"label": "꼼꼼해요"}, {"label": "시간 준수"},
    ],
}


def _fill_preview_data(
    template: str,
    slots: Dict[str, Dict[str, str]],
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """미리보기 모드: 템플릿에서 사용하는 슬롯에 더미 데이터를 자동 생성.

    1. {{#each X}} 패턴에서 X를 추출 → 데이터에 없으면 더미 리스트 주입
    2. {{slot_name}} 패턴에서 slot_name 추출 → 데이터에 없으면 더미 값 주입
    3. 컴포넌트 slots 정의 참조하여 타입별 더미 값 생성
    """
    filled = dict(data)

    # 1. {{#each X}} 블록의 X에 대해 더미 리스트 주입
    each_keys = re.findall(r"\{\{#each\s+([^}]+?)\}\}", template)
    for key in each_keys:
        key = key.strip()
        existing = _resolve_path(key, filled)
        if isinstance(existing, list) and len(existing) > 0:
            continue  # 이미 데이터 있음
        # 알려진 패턴에서 더미 데이터 찾기
        dummy_list = _PREVIEW_ROW_TEMPLATES.get(key)
        if not dummy_list:
            # 템플릿 body에서 _item.X 필드를 추출하여 동적 행 생성
            each_pattern = re.compile(
                r"\{\{#each\s+" + re.escape(key) + r"\}\}([\s\S]*?)\{\{/each\}\}"
            )
            m = each_pattern.search(template)
            if m:
                body = m.group(1)
                item_fields = re.findall(r"\{\{_item\.(\w+)\}\}", body)
                if item_fields:
                    # _item 필드에서 더미 행 생성
                    dummy_list = []
                    for i in range(3):
                        row: Dict[str, Any] = {}
                        for f in set(item_fields):
                            row[f] = _PREVIEW_SLOT_DEFAULTS.get(
                                f, f"샘플 {f} {i+1}"
                            )
                        # id는 순번으로
                        if "id" in row:
                            row["id"] = f"{i+1:03d}"
                        if "name" in row:
                            row["name"] = f"항목 {chr(65+i)}"
                        dummy_list.append(row)
        if dummy_list:
            filled[key] = dummy_list

    # 2. {{slot_name}} 단순 슬롯에 더미 값 주입
    simple_slots = re.findall(r"\{\{([^#/][^}]*?)\}\}", template)
    for slot_expr in simple_slots:
        slot_name = slot_expr.strip()
        if slot_name.startswith("_item"):
            continue  # each 블록 내부 변수는 스킵
        existing = _resolve_path(slot_name, filled)
        if existing is not None:
            continue
        # 기본 더미 값
        default = _PREVIEW_SLOT_DEFAULTS.get(slot_name)
        if default is not None:
            filled[slot_name] = default
        elif "." in slot_name:
            # dot path: parent.child → parent dict 생성
            parts = slot_name.split(".")
            leaf = parts[-1]
            default = _PREVIEW_SLOT_DEFAULTS.get(leaf, "")
            # 중첩 dict 생성
            current = filled
            for part in parts[:-1]:
                if part not in current or not isinstance(current.get(part), dict):
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = default

    # 3. 컴포넌트 slots 정의에서 빈 슬롯 채우기
    for slot_name, slot_def in slots.items():
        if slot_name in filled and filled[slot_name] is not None:
            continue
        slot_type = slot_def.get("type", "text") if isinstance(slot_def, dict) else "text"
        if slot_type == "list":
            filled[slot_name] = _PREVIEW_ROW_TEMPLATES.get(
                slot_name, [{"label": f"항목 {i+1}"} for i in range(3)]
            )
        elif slot_type in ("number", "integer"):
            filled[slot_name] = _PREVIEW_SLOT_DEFAULTS.get(slot_name, 0)
        elif slot_type == "boolean":
            filled[slot_name] = True
        else:
            # icon/action 계열 슬롯은 빈 문자열 기본값 (변수명 노출 방지)
            if any(k in slot_name for k in ("icon", "button", "action", "embed", "svg", "sparkline", "badge")):
                filled[slot_name] = _PREVIEW_SLOT_DEFAULTS.get(slot_name, "")
            else:
                filled[slot_name] = _PREVIEW_SLOT_DEFAULTS.get(slot_name, "")

    return filled


# ---------------------------------------------------------------------------
# 슬롯 렌더링
# ---------------------------------------------------------------------------

def _render_slots(template: str, data: Dict[str, Any]) -> str:
    """컴포넌트 HTML 템플릿의 {{slot}} 플레이스홀더를 데이터로 치환.

    지원 패턴:
      {{slot_name}}          — 단순 치환
      {{_item.field}}        — 반복 렌더링 시 아이템 필드
      {{#if slot_name}}...{{/if}}   — 조건부 블록
      {{#each items}}...{{/each}}   — 반복 블록
    """
    # 1. 조건부 블록
    template = _process_conditionals(template, data)

    # 2. 반복 블록
    template = _process_each_blocks(template, data)

    # 3. 단순 슬롯 치환
    # HTML 값을 이스케이프 없이 삽입할 슬롯 패턴
    _RAW_HTML_SLOTS = {"logo", "chart_svg", "map_embed", "embed", "raw_html", "svg", "icon_html"}

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        value = _resolve_path(key, data)
        if value is None:
            return ""
        if isinstance(value, dict):
            for dk in ("name", "label", "title", "text", "value"):
                if dk in value:
                    return _escape_html(str(value[dk]))
            return ""
        if isinstance(value, list):
            parts = []
            for v in value:
                if isinstance(v, dict):
                    for dk in ("name", "label", "title", "text"):
                        if dk in v:
                            parts.append(str(v[dk]))
                            break
                else:
                    parts.append(str(v))
            return ", ".join(parts)
        s = str(value)
        # HTML 콘텐츠를 포함한 슬롯은 이스케이프하지 않음
        leaf = key.split(".")[-1] if "." in key else key
        if leaf in _RAW_HTML_SLOTS or s.startswith("<"):
            return s
        return _escape_html(s)

    result = re.sub(r"\{\{([^#/][^}]*?)\}\}", _replace, template)

    # 4. 단일 중괄호 {path.variable} 도 치환 (레시피 바인딩에서 생성되는 패턴)
    def _replace_single(match: re.Match) -> str:
        key = match.group(1).strip()
        value = _resolve_path(key, data)
        if value is None:
            return ""
        return _escape_html(str(value))

    result = re.sub(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_.]+)\}(?!\})", _replace_single, result)

    return result


def _process_conditionals(template: str, data: Dict[str, Any]) -> str:
    """{{#if key}}...{{/if}} 블록 처리 (중첩 대응: 안쪽부터 반복)."""
    inner_pattern = r"\{\{#if\s+([^}]+?)\}\}((?:(?!\{\{#if)[\s\S])*?)\{\{/if\}\}"

    for _ in range(10):
        match = re.search(inner_pattern, template)
        if not match:
            break

        def _replace(m: re.Match) -> str:
            key = m.group(1).strip()
            content = m.group(2)
            value = _resolve_path(key, data)
            if value:
                return content
            return ""

        template = re.sub(inner_pattern, _replace, template)

    return template


def _process_each_blocks(template: str, data: Dict[str, Any]) -> str:
    """{{#each items}}...{{/each}} 블록 처리.

    outer-first 접근: 첫 번째 {{#each}}를 찾고 매칭 {{/each}}를 nesting 카운트로
    찾은 뒤, 각 아이템에 대해 body를 재귀적으로 처리. 중첩 each도 정상 동작.
    """
    open_tag = re.compile(r"\{\{#each\s+([^}]+?)\}\}")
    close_tag = re.compile(r"\{\{/each\}\}")
    max_iterations = 30  # 최상위 블록 수 제한

    for _ in range(max_iterations):
        m_open = open_tag.search(template)
        if not m_open:
            break

        key = m_open.group(1).strip()
        # nesting 카운트로 매칭 {{/each}} 찾기
        depth = 1
        pos = m_open.end()
        body_end = -1
        block_end = -1
        while depth > 0 and pos < len(template):
            next_open = open_tag.search(template, pos)
            next_close = close_tag.search(template, pos)
            if next_close is None:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                pos = next_open.end()
            else:
                depth -= 1
                if depth == 0:
                    body_end = next_close.start()
                    block_end = next_close.end()
                pos = next_close.end()

        if body_end < 0:
            # 매칭 실패 — 깨진 템플릿, 무한루프 방지
            break

        body = template[m_open.end():body_end]
        items = _resolve_path(key, data)

        if not isinstance(items, list):
            logger.debug("each_block_empty key=%s type=%s", key, type(items).__name__)
            template = template[:m_open.start()] + template[block_end:]
            continue

        parts = []
        for i, item in enumerate(items):
            if isinstance(item, dict):
                item_data = {**data, **item, "_item": item, "_index": i}
            else:
                item_data = {**data, "_item": item, "_index": i}
            rendered = _process_conditionals(body, item_data)
            # 중첩 each를 먼저 처리해야 inner {{_item}}이 outer 값으로 치환되지 않음
            rendered = _process_each_blocks(rendered, item_data)
            rendered = _replace_simple_slots(rendered, item_data)
            parts.append(rendered)

        template = template[:m_open.start()] + "".join(parts) + template[block_end:]

    return template


def _replace_simple_slots(template: str, data: Dict[str, Any]) -> str:
    """단순 {{slot}} 치환만 수행 (each/if 블록은 건드리지 않음)."""
    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        value = _resolve_path(key, data)
        if value is None:
            return match.group(0)  # 미해결 슬롯은 그대로 유지
        if isinstance(value, dict):
            for dk in ("name", "label", "title", "text", "value"):
                if dk in value:
                    return _escape_html(str(value[dk]))
            return ""
        if isinstance(value, list):
            parts = []
            for v in value:
                if isinstance(v, dict):
                    for dk in ("name", "label", "title", "text"):
                        if dk in v:
                            parts.append(str(v[dk]))
                            break
                else:
                    parts.append(str(v))
            return ", ".join(parts)
        return _escape_html(str(value))

    return re.sub(r"\{\{([^#/][^}]*?)\}\}", _replace, template)


def _render_component(
    comp: Component,
    placement: ComponentPlacement,
    data_ctx: Dict[str, Any],
    preview_mode: bool = False,
) -> str:
    """컴포넌트 + 바인딩 → 렌더링된 HTML 조각."""
    # 바인딩에서 슬롯 값 구성
    slot_data: Dict[str, Any] = {}

    for binding in placement.bindings:
        value = _resolve_binding(binding, data_ctx)
        if binding.transform:
            value = _apply_transform(value, binding.transform)
        # None 값은 data_ctx를 덮어쓰지 않음 (기존 데이터 보존)
        if value is not None:
            slot_data[binding.slot_name] = value

    # data_context의 값도 슬롯으로 사용 가능 (바인딩이 우선)
    merged = {**data_ctx, **slot_data}

    # 미리보기 모드: 템플릿에서 사용하는 슬롯에 더미 데이터 자동 생성
    if preview_mode:
        merged = _fill_preview_data(comp.html_template, comp.slots, merged)

    return _render_slots(comp.html_template, merged)


def _resolve_binding(binding: SlotBinding, data_ctx: Dict[str, Any]) -> Any:
    """슬롯 바인딩 값 해석."""
    # 정적 값 우선
    if binding.value is not None:
        return binding.value

    # 데이터 컨텍스트에서 경로 해석
    if binding.source_path:
        return _resolve_path(binding.source_path, data_ctx)

    # source_artifact는 executor 연동 시 처리 (Phase 3)
    return None


def _resolve_path(path: str, data: Dict[str, Any]) -> Any:
    """점 표기법으로 중첩 데이터 접근.

    예: "project.stats.total" → data["project"]["stats"]["total"]
        "items[0].name" → data["items"][0]["name"]
    """
    if not path or not data:
        return None

    parts = re.split(r"\.|\[(\d+)\]", path)
    parts = [p for p in parts if p is not None and p != ""]

    current: Any = data
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _apply_transform(value: Any, transform: str) -> Any:
    """슬롯 값에 변환 적용.

    지원 변환:
      - markdown_to_html: 마크다운 → HTML (간이)
      - truncate:N: 문자열을 N자로 자르기
      - upper / lower: 대소문자 변환
      - number_format: 숫자 포맷 (천 단위 콤마)
      - date_format: ISO → 한국어 날짜
    """
    if value is None:
        return ""

    if transform.startswith("truncate:"):
        try:
            n = int(transform.split(":")[1])
            s = str(value)
            return s[:n] + ("..." if len(s) > n else "")
        except (ValueError, IndexError):
            return str(value)

    if transform == "upper":
        return str(value).upper()

    if transform == "lower":
        return str(value).lower()

    if transform == "number_format":
        try:
            return f"{int(value):,}"
        except (ValueError, TypeError):
            return str(value)

    if transform == "date_format":
        # ISO 8601 → "2026년 3월 28일"
        s = str(value)
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if match:
            return f"{match.group(1)}년 {int(match.group(2))}월 {int(match.group(3))}일"
        return s

    if transform == "markdown_to_html":
        return _simple_markdown_to_html(str(value))

    return value


def _simple_markdown_to_html(md: str) -> str:
    """최소한의 마크다운 → HTML 변환 (외부 의존성 없음)."""
    html = _escape_html(md)
    # 헤딩
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    # 볼드, 이탤릭
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # 리스트
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    # 줄바꿈
    html = html.replace("\n\n", "</p><p>")
    html = f"<p>{html}</p>"
    return html


def _evaluate_condition(condition: str, data: Dict[str, Any]) -> bool:
    """간단한 조건식 평가.

    지원:
      "if data.items"       → data.items가 truthy
      "unless empty"        → 비어있지 않으면 True
      "if data.count > 0"   → 비교
    """
    condition = condition.strip()

    # "if X" / "unless X"
    negate = False
    if condition.startswith("unless "):
        negate = True
        condition = condition[7:]
    elif condition.startswith("if "):
        condition = condition[3:]

    # 비교 연산
    for op in (" > ", " < ", " >= ", " <= ", " == ", " != "):
        if op in condition:
            left_path, right_val = condition.split(op, 1)
            left = _resolve_path(left_path.strip(), data)
            try:
                right = type(left)(right_val.strip()) if left is not None else right_val.strip()
            except (ValueError, TypeError):
                right = right_val.strip()

            result = False
            if left is None:
                result = op.strip() in ("==", "!=") and left == right
            elif op.strip() == ">":
                result = left > right
            elif op.strip() == "<":
                result = left < right
            elif op.strip() == ">=":
                result = left >= right
            elif op.strip() == "<=":
                result = left <= right
            elif op.strip() == "==":
                result = left == right
            elif op.strip() == "!=":
                result = left != right

            return (not result) if negate else result

    # 단순 truthy 체크
    value = _resolve_path(condition, data)
    truthy = bool(value)
    return (not truthy) if negate else truthy


def _wrap_placement(html: str, placement: ComponentPlacement, suffix: str = "") -> str:
    """컴포넌트 HTML을 래퍼 div로 감싸기."""
    classes = f"comp-{placement.component_name}"
    if placement.wrapper_css_class:
        classes += f" {placement.wrapper_css_class}"
    if suffix:
        return f'<div class="{classes}" data-order="{placement.order}" data-idx="{suffix}">\n{html}\n</div>'
    return f'<div class="{classes}" data-order="{placement.order}">\n{html}\n</div>'


# ---------------------------------------------------------------------------
# HTML 조립
# ---------------------------------------------------------------------------

def _assemble_html(
    title: str,
    description: str,
    css: str,
    body: str,
    layout: str,
    js: str,
    meta: Dict[str, str],
) -> str:
    """최종 HTML 문서 조립."""
    lang = meta.get("lang", "ko")

    # 폰트 패밀리 추출 (토큰에서)
    import re as _re
    font_match = _re.search(r"--font-family:\s*'([^']+)'", css)
    font_name = font_match.group(1) if font_match else "Pretendard Variable"
    font_slug = font_name.replace(" ", "+")

    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_escape_html(title)}</title>
  {f'<meta name="description" content="{_escape_html(description)}">' if description else ''}
  <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css" />
  <style>
{css}
  </style>
</head>
<body>
  <div class="page-layout layout-{layout}">
{body}
  </div>
{f'  <script>{js}</script>' if js else ''}
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSS 헬퍼
# ---------------------------------------------------------------------------

def _base_reset_css() -> str:
    """최소한의 리셋 + 기본 타이포그래피."""
    return """/* base reset */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: var(--font-family, 'Pretendard', -apple-system, sans-serif);
  font-size: var(--font-size-body, 16px);
  line-height: var(--line-height, 1.6);
  color: var(--color-text, #1a1a1a);
  background: var(--color-bg, #ffffff);
  -webkit-font-smoothing: antialiased;
}
h1 { font-size: var(--font-size-h1, 2.5rem); }
h2 { font-size: var(--font-size-h2, 2rem); }
h3 { font-size: var(--font-size-h3, 1.5rem); }
h4 { font-size: var(--font-size-h4, 1.25rem); }
h5 { font-size: var(--font-size-h5, 1.1rem); }
img { max-width: 100%; height: auto; }
a { color: var(--color-primary, #2563EB); text-decoration: none; }
a:hover { text-decoration: underline; }"""


def _fallback_tokens_css() -> str:
    """토큰이 없을 때 사용하는 기본 CSS 변수."""
    return """:root {
  --color-primary: #2563EB;
  --color-secondary: #7C3AED;
  --color-bg: #ffffff;
  --color-surface: #f8fafc;
  --color-text: #1a1a1a;
  --color-text-muted: #64748b;
  --color-border: #e2e8f0;
  --font-family: 'Pretendard', -apple-system, sans-serif;
  --font-size-body: 16px;
  --line-height: 1.6;
  --font-size-h1: 2.5rem;
  --font-size-h2: 2rem;
  --font-size-h3: 1.5rem;
  --font-size-h4: 1.25rem;
  --font-size-h5: 1.1rem;
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
  --space-2xl: 48px;
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 16px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.07);
  --bp-mobile: 480px;
  --bp-tablet: 768px;
  --bp-desktop: 1024px;
}"""


def _layout_css(layout: str) -> str:
    """레이아웃 타입별 CSS."""
    base = """.page-layout { width: 100%; max-width: 1280px; margin: 0 auto; padding: var(--space-lg, 24px); }"""

    layouts = {
        "single-column": base,
        "sidebar-left": base + """
.layout-sidebar-left { display: grid; grid-template-columns: 280px 1fr; gap: var(--space-lg, 24px); }
@media (max-width: 768px) { .layout-sidebar-left { grid-template-columns: 1fr; } }""",
        "two-column": base + """
.layout-two-column { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-lg, 24px); }
@media (max-width: 768px) { .layout-two-column { grid-template-columns: 1fr; } }""",
        "grid": base + """
.layout-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: var(--space-lg, 24px); }""",
    }
    return layouts.get(layout, base)


def _preview_light_theme_css() -> str:
    """미리보기 모드: 다크 토큰을 라이트로 오버라이드 (시맨틱 변수 포함)."""
    return """/* preview light theme override */
:root {
  /* 기본 */
  --color-bg: #ffffff !important;
  --color-surface: #f8fafc !important;
  --color-surface-hover: #f1f5f9 !important;
  --color-text: #1a1a2e !important;
  --color-text-muted: #64748b !important;
  --color-text-dim: #94a3b8 !important;
  --color-border: #e2e8f0 !important;
  /* 시맨틱 bg/surface */
  --color-bg-primary: #ffffff !important;
  --color-bg-surface: #f8fafc !important;
  --color-bg-card: #ffffff !important;
  --color-surface-1: #f8fafc !important;
  --color-surface-2: #f1f5f9 !important;
  --color-surface-3: #e2e8f0 !important;
  /* 시맨틱 text */
  --color-text-primary: #1a1a2e !important;
  --color-text-secondary: #64748b !important;
  --color-text-inverse: #ffffff !important;
  /* border */
  --color-border-subtle: #e2e8f0 !important;
  /* legacy */
  --color-card-bg: #ffffff !important;
  --color-sidebar-bg: #f1f5f9 !important;
  --color-sidebar-text: #334155 !important;
  --color-header-bg: #ffffff !important;
  --color-header-text: #1a1a2e !important;
  --color-input-bg: #ffffff !important;
  --color-input-border: #d1d5db !important;
  --color-hover: #f1f5f9 !important;
  --color-table-header: #f8fafc !important;
  --color-table-stripe: #fafbfc !important;
  --color-footer-bg: #1e293b !important;
  --color-footer-text: #e2e8f0 !important;
}
body {
  background: #ffffff !important;
  color: #1a1a2e !important;
  font-family: var(--font-family-base, var(--font-family, sans-serif)) !important;
}
/* 모바일 메뉴/햄버거 숨기기 */
.page-header__mobile-nav,
.page-header__mobile-menu,
.page-header__hamburger { display: none !important; }
/* 네비 링크 가로 정렬 */
.page-header__nav { display: flex !important; align-items: center; gap: 8px; }
.page-header__nav-list { display: flex !important; list-style: none; gap: 4px; margin: 0; padding: 0; }
.page-header__nav a,
.page-header__nav-link { white-space: nowrap; }
/* 헤더 레이아웃 */
.page-header__inner { display: flex; align-items: center; gap: 16px; }
.page-header__actions { margin-left: auto; display: flex; align-items: center; gap: 12px; }
/* 사이드바 */
.sidebar { min-height: 100vh; }
/* 테이블 로딩 스켈레톤 숨기기 */
.data-table__loading { display: none !important; }
.data-table__td:empty { height: 40px; }
/* 카드 그림자 */
.stat-card, .claim-card, .billing-card, .care-plan, .client-card,
.schedule-card, .rating-form, .rating-widget, .assign-item,
.map-visit, .notice-card, .work-summary, .family-summary,
.insurance-claim-card { box-shadow: var(--shadow-md, 0 4px 6px rgba(0,0,0,0.07)); }
"""


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    """HTML 특수문자 이스케이프."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _error_page(message: str) -> str:
    """에러 페이지 HTML."""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>렌더링 오류</title>
  <style>
    body {{ font-family: sans-serif; display: flex; justify-content: center;
           align-items: center; min-height: 100vh; background: #fef2f2; }}
    .error {{ background: white; padding: 2rem; border-radius: 8px;
              border: 1px solid #fca5a5; max-width: 500px; }}
    .error h2 {{ color: #dc2626; margin-bottom: 0.5rem; }}
  </style>
</head>
<body>
  <div class="error">
    <h2>렌더링 오류</h2>
    <p>{_escape_html(message)}</p>
  </div>
</body>
</html>"""
