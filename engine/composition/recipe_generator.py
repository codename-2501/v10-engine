"""
engine/composition/recipe_generator.py
프로그래매틱 페이지 레시피 생성기 — AI 호출 0회, 토큰 0.

화면 목록 정의서 + 컴포넌트 라이브러리에서 규칙 기반으로 레시피를 조립.
범용 프로젝트 대응 (특정 도메인 하드코딩 없음).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from engine.composition.registry import (
    CompositionRegistry,
    PageRecipe,
    ComponentPlacement,
    SlotBinding,
    ensure_required_placements,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 화면 유형 판별 키워드
# ---------------------------------------------------------------------------
TYPE_KEYWORDS: Dict[str, List[str]] = {
    "dashboard": ["대시보드", "dashboard", "홈", "home", "모니터링", "통계", "현황"],
    "list": ["목록", "관리", "list", "조회", "검색"],
    "detail": ["상세", "detail", "보기", "정보", "프로필"],
    "form": ["등록", "작성", "수정", "신청", "form", "create", "edit", "입력", "추가"],
    "calendar": ["일정", "schedule", "calendar", "캘린더", "스케줄"],
    "landing": ["랜딩", "landing", "소개", "메인"],
    "chatbot": ["챗봇", "chatbot", "chat", "상담", "메시지"],
    "mypage": ["마이페이지", "mypage", "내 정보", "설정", "계정", "프로필 설정"],
    "rating": ["평가", "rating", "만족도", "리뷰", "피드백"],
}

# 유형 우선순위 (앞에 오는 것이 높은 우선순위 — 더 구체적인 타입)
_TYPE_PRIORITY = [
    "dashboard", "calendar", "chatbot", "rating",
    "mypage", "landing", "form", "detail", "list",
]

# ---------------------------------------------------------------------------
# 컴포넌트 배치 템플릿 (유형별)
# ---------------------------------------------------------------------------
# 각 항목: (component_name, order, [(slot_name, value_template), ...])

_ADMIN_PREFIX = "/admin"

PLACEMENT_TEMPLATES: Dict[str, List[Tuple[str, int, List[Tuple[str, str]]]]] = {
    "list": [
        ("sidebar", 1, []),
        ("page_header", 2, [
            ("title", "{screen_name}"),
            ("action_button_label", "신규 등록"),
            ("action_button_href", "/{slug}/create"),
        ]),
        ("stat_card", 3, [
            ("items", "auto"),
            ("columns", "4"),
        ]),
        ("search_bar", 4, [
            ("placeholder", "{resource} 검색"),
        ]),
        ("data_table", 5, [
            ("columns", "auto"),
            ("rows", "data.items"),
            ("sortable", "true"),
            ("empty_message", "데이터가 없습니다."),
        ]),
        ("pagination", 6, [
            ("current_page", "page"),
            ("total_pages", "totalPages"),
            ("onPageChange", "handlePageChange"),
        ]),
    ],
    "detail": [
        ("sidebar", 1, []),
        ("breadcrumb", 2, [("items", "auto")]),
        ("page_header", 3, [("title", "{screen_name}")]),
        ("client_profile_card", 4, []),
        ("tab_bar", 5, [("tabs", "auto")]),
        ("care_log_viewer", 6, []),
    ],
    "form": [
        ("sidebar", 1, []),
        ("page_header", 2, [
            ("title", "{screen_name}"),
            ("back_href", "/{parent_slug}"),
        ]),
        ("info_panel", 3, []),
        ("form_fields", 4, []),
        ("page_footer", 5, []),
    ],
    "dashboard": [
        ("sidebar", 1, []),
        ("page_header", 2, [("title", "{screen_name}")]),
        ("stat_card", 3, [
            ("items", "auto"),
            ("columns", "4"),
        ]),
        ("stats_chart_card", 4, []),
        ("data_table", 5, [
            ("columns", "auto"),
            ("rows", "data.recent"),
            ("sortable", "true"),
            ("empty_message", "최근 데이터가 없습니다."),
        ]),
    ],
    "calendar": [
        ("page_header", 1, [("title", "{screen_name}")]),
        ("tab_bar", 2, [("tabs", "auto")]),
        ("calendar_widget", 3, []),
        ("schedule_card", 4, []),
        ("mobile_bottom_nav", 5, []),
    ],
    "landing": [
        ("page_header", 1, [("title", "{project_name}")]),
        ("hero_banner", 2, [
            ("title", "{project_name}"),
            ("subtitle", "{screen_description}"),
            ("cta_label", "시작하기"),
            ("cta_href", "/login"),
        ]),
        ("feature_grid", 3, [("items", "auto")]),
        ("cta_section", 4, []),
        ("page_footer", 5, []),
    ],
    "chatbot": [
        ("page_header", 1, [("title", "{screen_name}")]),
        ("chat_message_list", 2, []),
        ("chat_input_bar", 3, []),
    ],
    "mypage": [
        ("page_header", 1, [("title", "{screen_name}")]),
        ("client_profile_card", 2, []),
        ("settings_form", 3, []),
    ],
    "rating": [
        ("sidebar", 1, []),
        ("page_header", 2, [("title", "{screen_name}")]),
        ("rating_summary", 3, []),
        ("data_table", 4, [
            ("columns", "auto"),
            ("rows", "data.reviews"),
            ("empty_message", "평가 내역이 없습니다."),
        ]),
    ],
}

# FO(프론트) 페이지 기본 — sidebar 대신 header/footer
_FO_HEADER_FOOTER: Dict[str, List[Tuple[str, int, List[Tuple[str, str]]]]] = {
    "_header": ("page_header", 0, [("title", "{screen_name}")]),
    "_footer": ("page_footer", 999, []),
}

# ---------------------------------------------------------------------------
# 아이콘 추론
# ---------------------------------------------------------------------------
_ICON_MAP: Dict[str, str] = {
    "대시보드": "dashboard", "홈": "home", "home": "home",
    "관리": "settings", "목록": "list", "설정": "settings",
    "일정": "calendar", "캘린더": "calendar", "스케줄": "calendar",
    "통계": "bar_chart", "차트": "bar_chart", "모니터링": "monitoring",
    "알림": "notifications", "공지": "campaign",
    "사용자": "person", "고객": "person", "요양": "favorite",
    "보호사": "people", "어르신": "elderly", "상담": "chat",
    "챗봇": "chat", "메시지": "message", "게시판": "forum",
    "매칭": "handshake", "리뷰": "star", "평가": "star",
    "급여": "payments", "결제": "credit_card", "정산": "receipt",
    "문서": "description", "파일": "folder",
}


def _infer_icon(name: str) -> str:
    """화면 이름에서 아이콘 추론."""
    for keyword, icon in _ICON_MAP.items():
        if keyword in name:
            return icon
    return "article"


# ---------------------------------------------------------------------------
# 화면 목록 파싱
# ---------------------------------------------------------------------------
_SCR_TABLE_RE = re.compile(
    r"SCR-(\d{3})\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+)",
)


def _parse_screen_list(content: str) -> List[Dict[str, str]]:
    """화면 목록 정의서 텍스트에서 화면 목록을 파싱.

    Returns:
        [{"id": "SCR-001", "name": "...", "url": "...", "type": "...",
          "description": "..."}, ...]
    """
    screens = []
    seen_ids = set()

    for m in _SCR_TABLE_RE.finditer(content):
        scr_id = f"SCR-{m.group(1)}"
        if scr_id in seen_ids:
            continue
        seen_ids.add(scr_id)

        name = m.group(2).strip()
        url = m.group(3).strip()
        stype = m.group(4).strip()

        # 5번째 컬럼이 있으면 description
        desc = ""
        rest = content[m.end():]
        desc_match = re.match(r"\s*[|｜]\s*([^|｜\n]*)", rest)
        if desc_match:
            desc = desc_match.group(1).strip()

        screens.append({
            "id": scr_id,
            "name": name,
            "url": url,
            "type": stype,
            "description": desc or name,
        })

    if not screens:
        # 폴백: 간략 테이블
        _simple = re.compile(r"SCR-(\d{3})\s*[|｜]\s*([^|｜\n]+)")
        for m2 in _simple.finditer(content):
            scr_id = f"SCR-{m2.group(1)}"
            if scr_id in seen_ids:
                continue
            seen_ids.add(scr_id)
            screens.append({
                "id": scr_id,
                "name": m2.group(2).strip(),
                "url": "",
                "type": "",
                "description": m2.group(2).strip(),
            })

    return screens


# ---------------------------------------------------------------------------
# 유형 판별
# ---------------------------------------------------------------------------


def _detect_screen_type(screen: Dict[str, str]) -> str:
    """화면 정보에서 유형을 판별.

    판별 우선순위:
    1. 화면 목록의 type 컬럼 직접 매칭
    2. 화면 이름 + URL 키워드 매칭
    3. 폴백: list
    """
    raw_type = screen.get("type", "").strip().lower()
    name = screen.get("name", "").lower()
    url = screen.get("url", "").lower()
    combined = f"{raw_type} {name} {url}"

    for stype in _TYPE_PRIORITY:
        for keyword in TYPE_KEYWORDS[stype]:
            if keyword.lower() in combined:
                return stype

    # 폴백
    return "list"


def _name_to_slug(name: str) -> str:
    """한글/영문 화면 이름을 URL-safe slug로 변환."""
    # URL에서 슬러그 추출 시도
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", name.lower()).strip("-")
    # 한글 → 영문 매핑 (주요 패턴)
    _KO_SLUG = {
        "대시보드": "dashboard", "홈": "home", "목록": "list",
        "관리": "manage", "상세": "detail", "등록": "create",
        "작성": "create", "수정": "edit", "검색": "search",
        "설정": "settings", "마이페이지": "mypage", "로그인": "login",
        "일정": "schedule", "캘린더": "calendar", "알림": "notifications",
        "공지사항": "notices", "게시판": "board", "통계": "stats",
        "결제": "payment", "정산": "settlement", "리뷰": "reviews",
        "평가": "rating", "상담": "consultation", "챗봇": "chatbot",
        "메시지": "messages", "프로필": "profile", "소개": "intro",
    }
    result = slug
    for ko, en in _KO_SLUG.items():
        result = result.replace(ko, en)
    # 남은 한글 제거
    result = re.sub(r"[가-힣]+", "", result)
    result = re.sub(r"-+", "-", result).strip("-")
    return result or f"page-{uuid.uuid4().hex[:6]}"


def _url_to_slug(url: str) -> str:
    """URL 경로에서 slug 추출."""
    if not url or url == "/" or url == "#":
        return ""
    # /admin/caregivers → admin-caregivers
    path = url.strip("/").replace("/", "-")
    path = re.sub(r"[^a-z0-9-]", "", path.lower())
    return path or ""


def _slug_to_resource(slug: str) -> str:
    """slug에서 리소스 이름 추출 (마지막 세그먼트)."""
    parts = slug.split("-")
    # admin 제거
    if parts and parts[0] == "admin":
        parts = parts[1:]
    return parts[-1] if parts else slug


def _is_admin_screen(screen: Dict[str, str]) -> bool:
    """관리자(BO) 화면인지 판별."""
    url = screen.get("url", "")
    name = screen.get("name", "")
    return (
        url.startswith("/admin")
        or "관리자" in name
        or "백오피스" in name
        or "BO" in screen.get("type", "")
    )


# ---------------------------------------------------------------------------
# 바인딩 생성 헬퍼
# ---------------------------------------------------------------------------


def _build_sidebar_menu(
    all_screens: List[Dict[str, str]],
    current_slug: str,
) -> List[Dict[str, Any]]:
    """전체 화면 목록에서 사이드바 메뉴 구성."""
    admin_screens = [s for s in all_screens if _is_admin_screen(s)]
    # 관리자 화면이 없으면 전체 사용
    target = admin_screens if admin_screens else all_screens

    menu = []
    for s in target:
        s_slug = _url_to_slug(s["url"]) or _name_to_slug(s["name"])
        s_type = _detect_screen_type(s)
        # 폼/상세 화면은 메뉴에서 제외 (보통 네비게이션에서 직접 접근하지 않음)
        if s_type in ("form", "detail"):
            continue
        menu.append({
            "icon": _infer_icon(s["name"]),
            "label": s["name"],
            "href": s.get("url") or f"/{s_slug}",
            "active": s_slug == current_slug,
        })
    return menu


def _infer_table_columns(resource: str, screen_name: str) -> List[Dict[str, str]]:
    """리소스와 화면명에서 테이블 컬럼을 추론."""
    # 공통 컬럼 패턴
    base = [
        {"key": "name", "label": "이름"},
        {"key": "status", "label": "상태", "render": "status_badge"},
        {"key": "created_at", "label": "등록일", "transform": "date_format"},
        {"key": "actions", "label": "관리", "render": "action_buttons"},
    ]

    # 키워드별 컬럼 커스터마이즈
    name_lower = screen_name.lower()
    if any(kw in name_lower for kw in ("공지", "게시", "notice", "board")):
        return [
            {"key": "title", "label": "제목"},
            {"key": "category", "label": "분류"},
            {"key": "author", "label": "작성자"},
            {"key": "created_at", "label": "작성일", "transform": "date_format"},
            {"key": "views", "label": "조회수"},
        ]
    if any(kw in name_lower for kw in ("결제", "정산", "payment", "settlement")):
        return [
            {"key": "id", "label": "번호"},
            {"key": "description", "label": "내용"},
            {"key": "amount", "label": "금액", "transform": "number_format"},
            {"key": "status", "label": "상태", "render": "status_badge"},
            {"key": "date", "label": "일자", "transform": "date_format"},
        ]
    if any(kw in name_lower for kw in ("일정", "schedule", "calendar")):
        return [
            {"key": "title", "label": "일정명"},
            {"key": "date", "label": "날짜", "transform": "date_format"},
            {"key": "time", "label": "시간"},
            {"key": "status", "label": "상태", "render": "status_badge"},
        ]

    return base


def _infer_stat_items(screen_name: str, screen_type: str) -> List[Dict[str, Any]]:
    """화면에 맞는 통계 카드 항목 추론."""
    name_lower = screen_name.lower()

    if screen_type == "dashboard":
        return [
            {"label": "전체", "value": "128", "change": "+12%", "icon": "people"},
            {"label": "신규", "value": "23", "change": "+5", "icon": "person_add"},
            {"label": "활성", "value": "95", "change": "+8%", "icon": "check_circle"},
            {"label": "대기", "value": "10", "change": "-2", "icon": "pending"},
        ]

    # 목록 화면용 요약 통계
    return [
        {"label": "전체", "value": "256", "icon": "list"},
        {"label": "활성", "value": "198", "icon": "check"},
        {"label": "대기", "value": "42", "icon": "hourglass"},
        {"label": "완료", "value": "16", "icon": "done_all"},
    ]


def _infer_breadcrumb(screen: Dict[str, str], all_screens: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """화면의 계층 구조에서 breadcrumb 추론."""
    url = screen.get("url", "")
    parts = [p for p in url.strip("/").split("/") if p]
    crumbs = [{"label": "홈", "href": "/"}]

    # URL 경로를 따라 상위 화면 찾기
    path_acc = ""
    for i, part in enumerate(parts[:-1]):
        path_acc += f"/{part}"
        # 상위 경로에 해당하는 화면 찾기
        parent = next((s for s in all_screens if s.get("url", "").rstrip("/") == path_acc), None)
        if parent:
            crumbs.append({"label": parent["name"], "href": path_acc})
        else:
            crumbs.append({"label": part.title(), "href": path_acc})

    crumbs.append({"label": screen["name"], "href": url})
    return crumbs


def _infer_tabs(screen_name: str, screen_type: str) -> List[Dict[str, str]]:
    """화면 유형에 맞는 탭 추론."""
    if screen_type == "detail":
        return [
            {"key": "overview", "label": "개요"},
            {"key": "history", "label": "이력"},
            {"key": "documents", "label": "문서"},
        ]
    if screen_type == "calendar":
        return [
            {"key": "month", "label": "월간"},
            {"key": "week", "label": "주간"},
            {"key": "day", "label": "일간"},
        ]
    if screen_type == "mypage":
        return [
            {"key": "profile", "label": "프로필"},
            {"key": "settings", "label": "설정"},
            {"key": "notifications", "label": "알림"},
        ]
    return []


def _find_parent_slug(screen: Dict[str, str], all_screens: List[Dict[str, str]]) -> str:
    """폼 화면의 부모(목록) slug 추론."""
    url = screen.get("url", "")
    # /admin/caregivers/create → /admin/caregivers
    parent_url = "/".join(url.rstrip("/").split("/")[:-1])
    if parent_url:
        parent = next(
            (s for s in all_screens if s.get("url", "").rstrip("/") == parent_url),
            None,
        )
        if parent:
            return _url_to_slug(parent["url"]) or _name_to_slug(parent["name"])
    return ""


# ---------------------------------------------------------------------------
# 배치 생성 핵심
# ---------------------------------------------------------------------------


def _resolve_placement_name(
    template_name: str,
    available_components: List[str],
) -> str:
    """템플릿 컴포넌트명을 실제 등록된 컴포넌트명으로 매핑.

    정확한 이름 매칭 → 부분 매칭 → 원본 유지 (렌더러가 fallback 처리)
    """
    if template_name in available_components:
        return template_name

    # 부분 매칭 (예: 'data_table' → 'admin_data_table')
    for comp in available_components:
        if template_name in comp or comp in template_name:
            return comp

    # 언더스코어/하이픈 차이 허용
    norm_template = template_name.replace("-", "_")
    for comp in available_components:
        if comp.replace("-", "_") == norm_template:
            return comp

    return template_name


def _build_bindings(
    binding_defs: List[Tuple[str, str]],
    screen: Dict[str, str],
    all_screens: List[Dict[str, str]],
    project_name: str,
    slug: str,
    screen_type: str,
    available_components: List[str],
) -> List[SlotBinding]:
    """바인딩 정의 템플릿에서 실제 SlotBinding 리스트 생성."""
    resource = _slug_to_resource(slug)
    parent_slug = _find_parent_slug(screen, all_screens)
    bindings = []

    for slot_name, value_template in binding_defs:
        value: Any = value_template

        # 템플릿 변수 치환
        if isinstance(value, str):
            value = (
                value
                .replace("{screen_name}", screen.get("name", ""))
                .replace("{screen_description}", screen.get("description", screen.get("name", "")))
                .replace("{project_name}", project_name)
                .replace("{slug}", slug)
                .replace("{resource}", resource)
                .replace("{parent_slug}", parent_slug or slug)
            )

        # 'auto' 값 → 타입별 자동 생성
        if value == "auto":
            if slot_name == "columns":
                value = _infer_table_columns(resource, screen.get("name", ""))
            elif slot_name == "items" and "stat" in (slot_name + value_template):
                value = _infer_stat_items(screen.get("name", ""), screen_type)
            elif slot_name == "items" and "breadcrumb" in slot_name:
                value = _infer_breadcrumb(screen, all_screens)
            elif slot_name == "items":
                value = _infer_stat_items(screen.get("name", ""), screen_type)
            elif slot_name == "tabs":
                value = _infer_tabs(screen.get("name", ""), screen_type)

        bindings.append(SlotBinding(slot_name=slot_name, value=value))

    return bindings


def _build_recipe_for_screen(
    screen: Dict[str, str],
    screen_type: str,
    all_screens: List[Dict[str, str]],
    available_components: List[str],
    project_name: str,
) -> PageRecipe:
    """단일 화면에 대한 레시피 생성."""
    slug = _url_to_slug(screen.get("url", "")) or _name_to_slug(screen["name"])
    is_admin = _is_admin_screen(screen)
    template = PLACEMENT_TEMPLATES.get(screen_type, PLACEMENT_TEMPLATES["list"])

    placements: List[ComponentPlacement] = []

    for comp_name, order, binding_defs in template:
        # FO 화면에서 sidebar 제거 + header/footer 추가
        if comp_name == "sidebar" and not is_admin:
            continue

        resolved_name = _resolve_placement_name(comp_name, available_components)

        # 사이드바 특수 바인딩
        if comp_name == "sidebar":
            sidebar_bindings = [
                SlotBinding(slot_name="logo_text", value=project_name),
                SlotBinding(
                    slot_name="menu_items",
                    value=_build_sidebar_menu(all_screens, slug),
                ),
            ]
            placements.append(ComponentPlacement(
                component_name=resolved_name,
                order=order,
                bindings=sidebar_bindings,
            ))
            continue

        bindings = _build_bindings(
            binding_defs, screen, all_screens,
            project_name, slug, screen_type,
            available_components,
        )

        placements.append(ComponentPlacement(
            component_name=resolved_name,
            order=order,
            bindings=bindings,
        ))

    # FO 화면: header(order=0) + footer(order=999) 자동 추가
    if not is_admin:
        header_name = _resolve_placement_name("page_header", available_components)
        footer_name = _resolve_placement_name("page_footer", available_components)
        # header가 이미 template에 있으면 order=0으로 조정
        has_header = any(p.component_name == header_name and p.order <= 1 for p in placements)
        if not has_header:
            placements.insert(0, ComponentPlacement(
                component_name=header_name,
                order=0,
                bindings=[SlotBinding(slot_name="title", value=screen["name"])],
            ))
        # footer
        has_footer = any("footer" in p.component_name for p in placements)
        if not has_footer:
            placements.append(ComponentPlacement(
                component_name=footer_name,
                order=999,
                bindings=[],
            ))

    # 레이아웃 결정
    layout = "single-column"
    if is_admin and screen_type not in ("landing", "chatbot"):
        layout = "sidebar-left"
    if screen_type == "dashboard":
        layout = "sidebar-left" if is_admin else "grid"

    recipe = PageRecipe(
        id=str(uuid.uuid4()),
        project_id="",  # 호출자가 설정
        page_name=screen["name"],
        page_slug=slug,
        title=screen["name"],
        description=screen.get("description", screen["name"]),
        layout=layout,
        placements=placements,
    )

    # 필수 UX placement 추가
    recipe = ensure_required_placements(recipe)

    return recipe


# ---------------------------------------------------------------------------
# 메인 API
# ---------------------------------------------------------------------------


async def generate_recipes_programmatic(
    db: Any,
    project_id: str,
) -> List[Dict]:
    """화면 목록 정의서 + 컴포넌트 라이브러리에서 레시피를 프로그래매틱 생성.

    AI 호출 0회, 토큰 0.

    Steps:
    1. Load 화면 목록 정의서 artifact
    2. Parse all screens
    3. Load available components from composition_components
    4. For each screen, generate recipe based on type
    5. Save each recipe to composition_recipes table
    6. Return list of generated recipe info

    Returns:
        [{"page_slug": ..., "page_name": ..., "screen_type": ...}, ...]
        빈 리스트이면 AI 폴백이 필요하다는 의미.
    """
    # 1. 화면 목록 정의서 로드
    row = await db.fetchone(
        """SELECT av.storage_path FROM artifacts a
           JOIN artifact_versions av ON a.id=av.artifact_id
           WHERE a.project_id=? AND a.node_id IN
             (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
           ORDER BY av.version_num DESC LIMIT 1""",
        (project_id, project_id),
    )
    if not row or not row["storage_path"]:
        logger.warning("recipe_gen_no_screen_list project=%s", project_id)
        return []

    screen_content = row["storage_path"]

    # 2. 화면 파싱
    screens = _parse_screen_list(screen_content)
    if not screens:
        logger.warning("recipe_gen_no_screens_parsed project=%s", project_id)
        return []

    # 3. 프로젝트 이름 로드
    project_row = await db.fetchone(
        "SELECT name FROM projects WHERE id=?",
        (project_id,),
    )
    project_name = project_row["name"] if project_row else "프로젝트"

    # 4. 등록된 컴포넌트 이름 목록
    comp_rows = await db.fetchall(
        "SELECT name FROM composition_components WHERE project_id=? ORDER BY name",
        (project_id,),
    )
    available_components = [r["name"] for r in comp_rows]

    if not available_components:
        logger.warning("recipe_gen_no_components project=%s", project_id)
        return []

    # 5. 레시피 생성 + 저장
    registry = CompositionRegistry(db)
    results = []

    for screen in screens:
        screen_type = _detect_screen_type(screen)
        recipe = _build_recipe_for_screen(
            screen, screen_type, screens,
            available_components, project_name,
        )
        recipe.project_id = project_id

        await registry.save_recipe(recipe)
        results.append({
            "page_slug": recipe.page_slug,
            "page_name": recipe.page_name,
            "screen_type": screen_type,
            "placement_count": len(recipe.placements),
        })

    logger.info(
        "recipe_gen_programmatic project=%s screens=%d recipes=%d",
        project_id, len(screens), len(results),
    )

    return results


def recipes_to_artifact_json(recipes_info: List[Dict], db_recipes: List) -> str:
    """생성된 레시피를 artifact 저장용 JSON 문자열로 변환.

    Args:
        recipes_info: generate_recipes_programmatic()의 반환값
        db_recipes: CompositionRegistry.load_all_recipes() 결과

    Returns:
        JSON 문자열 (페이지 레시피 배열)
    """
    from dataclasses import asdict
    output = []
    for recipe in db_recipes:
        d = asdict(recipe)
        # id, project_id는 메타데이터 → 산출물 JSON에서 제외
        d.pop("id", None)
        d.pop("project_id", None)
        d.pop("version", None)
        output.append(d)
    return json.dumps(output, ensure_ascii=False, indent=2)
