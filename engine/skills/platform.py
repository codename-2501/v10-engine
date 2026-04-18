"""
Platform detection and configuration for the Skill Executor.

Maps service_type + tech_preferences to concrete framework/styling/routing
configurations used during BUILD phase code generation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 플랫폼 감지 (service_type → 프레임워크/스타일링/라우팅)
# ---------------------------------------------------------------------------

PLATFORM_CONFIG = {
    # ── 웹 ──
    "web_nextjs": {
        "key": "web_nextjs",
        "framework": "Next.js (React + TypeScript)",
        "styling": "Tailwind CSS + CSS Variables (globals.css)",
        "routing": "App Router (app/ directory)",
        "component_ext": ".tsx",
        "infra_pattern": "globals.css + layout.tsx + providers.tsx",
    },
    "web_vue": {
        "key": "web_vue",
        "framework": "Nuxt 3 (Vue 3 + TypeScript)",
        "styling": "Tailwind CSS + CSS Variables",
        "routing": "Nuxt file-based routing (pages/ directory)",
        "component_ext": ".vue",
        "infra_pattern": "assets/main.css + layouts/default.vue + plugins/",
    },
    # ── 모바일 네이티브 ──
    "mobile_react_native": {
        "key": "mobile_react_native",
        "framework": "React Native (Expo + TypeScript)",
        "styling": "StyleSheet.create + Design Tokens (theme.ts)",
        "routing": "Expo Router (app/ directory)",
        "component_ext": ".tsx",
        "infra_pattern": "theme.ts + navigation.tsx + providers.tsx",
    },
    "mobile_flutter": {
        "key": "mobile_flutter",
        "framework": "Flutter (Dart)",
        "styling": "ThemeData + Custom Widgets",
        "routing": "GoRouter (declarative routing)",
        "component_ext": ".dart",
        "infra_pattern": "lib/theme/ + lib/routes/ + lib/providers/",
    },
    "mobile_swift": {
        "key": "mobile_swift",
        "framework": "SwiftUI (iOS native)",
        "styling": "SwiftUI Modifiers + Asset Catalog",
        "routing": "NavigationStack + NavigationPath",
        "component_ext": ".swift",
        "infra_pattern": "Theme.swift + Navigation/ + Services/",
    },
    "mobile_kotlin": {
        "key": "mobile_kotlin",
        "framework": "Jetpack Compose (Android native)",
        "styling": "MaterialTheme + Custom Composables",
        "routing": "Navigation Compose (NavHost)",
        "component_ext": ".kt",
        "infra_pattern": "ui/theme/ + navigation/ + di/",
    },
    # ── 하이브리드 ──
    "hybrid_rn_web": {
        "key": "hybrid_rn_web",
        "framework": "React Native Web (Expo + TypeScript)",
        "styling": "StyleSheet + Platform.select + CSS Variables",
        "routing": "Expo Router (app/ directory)",
        "component_ext": ".tsx",
        "infra_pattern": "theme.ts + layout.tsx + providers.tsx",
    },
    "hybrid_flutter_web": {
        "key": "hybrid_flutter_web",
        "framework": "Flutter Web + Mobile (Dart)",
        "styling": "ThemeData + Responsive Widgets",
        "routing": "GoRouter (declarative routing)",
        "component_ext": ".dart",
        "infra_pattern": "lib/theme/ + lib/routes/ + lib/responsive/",
    },
    # ── 데스크톱 ──
    "desktop_electron": {
        "key": "desktop_electron",
        "framework": "Electron (React + TypeScript)",
        "styling": "Tailwind CSS + CSS Variables",
        "routing": "React Router (hash routing)",
        "component_ext": ".tsx",
        "infra_pattern": "main.ts + preload.ts + renderer/",
    },
}

# 기본 매핑 (서비스 유형 → 기본 프레임워크, tech_preferences로 오버라이드 가능)
_DEFAULT_PLATFORM = {
    "web_service": "web_nextjs",
    "admin_backoffice": "web_nextjs",
    "mobile_app": "mobile_react_native",
    "hybrid": "hybrid_rn_web",
    "desktop": "desktop_electron",
}


_SERVICE_TYPE_NORMALIZE = {
    # 한국어 → 영어 정규화
    "웹 서비스": "web_service", "웹서비스": "web_service", "web": "web_service",
    "모바일 앱": "mobile_app", "모바일앱": "mobile_app", "mobile": "mobile_app",
    "네이티브 앱": "mobile_app", "네이티브앱": "mobile_app", "native": "mobile_app",
    "하이브리드": "hybrid", "hybrid": "hybrid",
    "백오피스": "admin_backoffice", "관리자": "admin_backoffice",
    "admin": "admin_backoffice", "backoffice": "admin_backoffice",
    "데스크톱": "desktop", "desktop": "desktop", "electron": "desktop",
    "AI 챗봇": "chatbot", "챗봇": "chatbot", "chatbot": "chatbot",
    "컨설팅": "consulting", "consulting": "consulting",
    # 영어 원본 (env_config_generator 호환)
    "web_service": "web_service", "mobile_app": "mobile_app",
    "admin_backoffice": "admin_backoffice",
}

# tech_preferences 텍스트에서 프레임워크 힌트 감지 (우선순위 순)
_TECH_HINT_MAP = [
    # 모바일 프레임워크 (구체적 → 범용 순서)
    ("swiftui",          "mobile_swift"),
    ("swift",            "mobile_swift"),
    ("ios native",       "mobile_swift"),
    ("ios 네이티브",      "mobile_swift"),
    ("jetpack compose",  "mobile_kotlin"),
    ("kotlin",           "mobile_kotlin"),
    ("android native",   "mobile_kotlin"),
    ("android 네이티브",  "mobile_kotlin"),
    ("flutter",          "mobile_flutter"),
    ("dart",             "mobile_flutter"),
    ("react native",     "mobile_react_native"),
    ("expo",             "mobile_react_native"),
    # 웹 프레임워크
    ("vue",              "web_vue"),
    ("nuxt",             "web_vue"),
    ("next.js",          "web_nextjs"),
    ("next",             "web_nextjs"),
    ("react",            "web_nextjs"),
    # 하이브리드
    ("flutter web",      "hybrid_flutter_web"),
    ("react native web", "hybrid_rn_web"),
    # 데스크톱
    ("electron",         "desktop_electron"),
    ("tauri",            "desktop_electron"),
]


def _detect_platform(global_context: dict) -> dict:
    """intake의 service_type + tech_preferences에서 타겟 플랫폼 감지.

    감지 우선순위:
      1. tech_preferences에 구체적 프레임워크 명시 → 해당 프레임워크
      2. service_type으로 카테고리 결정 → 기본 프레임워크
      3. 둘 다 없으면 → Next.js (기본값)
    """
    # service_type 정규화
    raw = global_context.get("service_type") or global_context.get("service_types") or []
    if isinstance(raw, str):
        raw = [raw]

    normalized = set()
    for s in raw:
        s_lower = s.strip().lower() if isinstance(s, str) else ""
        normalized.add(_SERVICE_TYPE_NORMALIZE.get(s, _SERVICE_TYPE_NORMALIZE.get(s_lower, s_lower)))

    has_web = bool(normalized & {"web_service", "admin_backoffice"})
    has_mobile = "mobile_app" in normalized
    has_desktop = "desktop" in normalized

    # 카테고리 결정
    if has_web and has_mobile:
        category = "hybrid"
    elif has_mobile:
        category = "mobile_app"
    elif has_desktop:
        category = "desktop"
    else:
        category = "web_service"

    # tech_preferences에서 프레임워크 힌트 감지
    tech_prefs = str(global_context.get("tech_preferences", "")).lower()

    if tech_prefs:
        for hint, platform_key in _TECH_HINT_MAP:
            if hint in tech_prefs:
                # 카테고리와 호환되는지 확인
                if platform_key in PLATFORM_CONFIG:
                    # hybrid인데 모바일 프레임워크 힌트 → 해당 hybrid 버전 탐색
                    if category == "hybrid" and platform_key.startswith("mobile_flutter"):
                        return PLATFORM_CONFIG.get("hybrid_flutter_web", PLATFORM_CONFIG[platform_key])
                    if category == "hybrid" and platform_key.startswith("mobile_react"):
                        return PLATFORM_CONFIG["hybrid_rn_web"]
                    return PLATFORM_CONFIG[platform_key]

    # 기본 매핑
    default_key = _DEFAULT_PLATFORM.get(category, "web_nextjs")
    return PLATFORM_CONFIG[default_key]


def _build_platform_instruction(platform: dict) -> str:
    """BUILD 프론트엔드 노드용 플랫폼 지시 블록."""
    return (
        f"\n\n## 타겟 플랫폼\n"
        f"- 프레임워크: {platform['framework']}\n"
        f"- 스타일링: {platform['styling']}\n"
        f"- 라우팅: {platform['routing']}\n"
        f"- 파일 확장자: {platform['component_ext']}\n"
        f"- 인프라 패턴: {platform['infra_pattern']}\n"
        f"- 조립된 HTML 페이지는 **시각 설계 참조**입니다. "
        f"이 디자인을 {platform['framework']}에 맞게 변환하세요.\n"
    )
