"""
통합 Admin CSS 프레임워크 — chunked HTML 섹션 간 시각 일관성 보장.

디자인 토큰 (:root 변수) + 공통 레이아웃 클래스 (nav/card/table/btn/modal)
+ 반응형 breakpoints 를 하나의 CSS 문자열로 생성.

각 chunk 는 이 프레임워크를 common_head 로 받아 HTML 만 생성.
고유 스타일이 필요하면 최소한의 <style> 만 추가.

사용:
    from engine.skills.design_framework import build_framework_css
    css = await build_framework_css(db, node)
    # → common_head 에 할당
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


async def build_framework_css(db: Any, node: Any) -> str:
    """디자인 토큰 + 공통 클래스 프레임워크 CSS 생성.

    Returns: <style>...</style> 문자열. 실패 시 기본 fallback CSS.
    """
    # 1. 디자인 토큰에서 :root 변수 추출
    token_vars = ""
    try:
        from engine.skills.executor import _build_style_from_design_tokens
        token_style = await _build_style_from_design_tokens(db, node)
        if token_style:
            # <style>...</style> 에서 내용만 추출
            m = re.search(r"<style[^>]*>([\s\S]*?)</style>", token_style)
            if m:
                token_vars = m.group(1).strip()
    except Exception as e:
        logger.debug("framework_token_extract_fail %s", e)

    if not token_vars:
        token_vars = _DEFAULT_TOKENS

    # 2. 공통 클래스 프레임워크 조립
    css = f"""
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');

{token_vars}

{_FRAMEWORK_CLASSES}

{_RESPONSIVE_BREAKPOINTS}
"""
    return f"<style>\n{css}\n</style>"


# ---------------------------------------------------------------------------
# 기본 토큰 (디자인 토큰 artifact 없을 때 fallback)
# ---------------------------------------------------------------------------

_DEFAULT_TOKENS = """
:root {
  --bg: #111318;
  --bg-2: #0d0f14;
  --surface: #1a1d24;
  --surface-2: #21252e;
  --surface-3: #282d38;
  --text: #eaedf2;
  --text-muted: #8b95a5;
  --text-dim: #5a6373;
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.15);
  --accent: #4f9cf7;
  --accent-dim: rgba(79,156,247,0.15);
  --success: #34d399;
  --success-dim: rgba(52,211,153,0.15);
  --warning: #fbbf24;
  --warning-dim: rgba(251,191,36,0.15);
  --danger: #f87171;
  --danger-dim: rgba(248,113,113,0.15);
  --info: #60a5fa;
  --info-dim: rgba(96,165,250,0.15);
  --radius: 12px;
  --radius-sm: 8px;
  --radius-lg: 16px;
  --radius-pill: 999px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.5);
  --font: 'Pretendard Variable', Pretendard, -apple-system, system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', monospace;
  --transition: 0.2s ease;
}
"""

# ---------------------------------------------------------------------------
# 공통 클래스 정의 (Admin UI 전용)
# ---------------------------------------------------------------------------

_FRAMEWORK_CLASSES = """
/* ===== Reset & Base ===== */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--font); line-height: 1.6; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ===== Admin Layout (Sidebar + Main) ===== */
.admin-layout, .admin-shell { display: flex; min-height: 100vh; }
.admin-nav, .admin-sidebar { width: 260px; background: var(--bg-2); border-right: 1px solid var(--border); padding: 20px 0; flex-shrink: 0; display: flex; flex-direction: column; }
.admin-main, .admin-content { flex: 1; padding: 24px 32px; overflow-x: hidden; }
.admin-topbar { display: flex; align-items: center; justify-content: space-between; padding: 12px 0 24px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }

/* ===== Nav Items ===== */
.nav-group { padding: 8px 0; }
.nav-title, .ad-nav-section { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim); padding: 8px 20px 4px; }
.nav-item, .ad-nav-item { display: flex; align-items: center; gap: 10px; padding: 8px 20px; color: var(--text-muted); font-size: 14px; border-radius: 0; transition: var(--transition); cursor: pointer; }
.nav-item:hover, .ad-nav-item:hover { background: var(--surface); color: var(--text); text-decoration: none; }
.nav-item.active, .ad-nav-item.active { background: var(--accent-dim); color: var(--accent); font-weight: 600; }
.nav-ico { width: 20px; text-align: center; font-size: 16px; }
.nav-badge { font-size: 10px; padding: 2px 6px; border-radius: var(--radius-pill); background: var(--danger-dim); color: var(--danger); margin-left: auto; }
.nav-badge.warn { background: var(--warning-dim); color: var(--warning); }
.nav-footer { margin-top: auto; padding: 16px 20px; border-top: 1px solid var(--border); }

/* ===== Brand ===== */
.admin-brand, .brand-header { display: flex; align-items: center; gap: 12px; padding: 8px 20px 20px; }
.brand-mark { width: 36px; height: 36px; border-radius: var(--radius-sm); background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; }
.brand-meta strong { display: block; font-size: 14px; color: var(--text); }
.brand-meta span { font-size: 12px; color: var(--text-muted); }

/* ===== User Chip ===== */
.user-chip { display: flex; align-items: center; gap: 10px; }
.chip-avatar { width: 32px; height: 32px; border-radius: 50%; background: var(--surface-3); display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; }
.chip-info strong { display: block; font-size: 13px; }
.chip-info span { font-size: 11px; color: var(--text-muted); }
.chip-logout { background: none; border: none; color: var(--text-dim); cursor: pointer; margin-left: auto; }

/* ===== Cards ===== */
.card, .kpi-card, .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }
.card:hover { border-color: var(--border-strong); }
.kpi-card { text-align: center; }
.kpi-card .value, .stat-value { font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
.kpi-card .label, .stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.kpi-card .trend { font-size: 12px; margin-top: 8px; }
.kpi-card .trend.up { color: var(--success); }
.kpi-card .trend.down { color: var(--danger); }

/* ===== Grid Layouts ===== */
.grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.grid-auto { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }

/* ===== Data Tables ===== */
table, .data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
table th, .data-table th { text-align: left; padding: 10px 12px; border-bottom: 2px solid var(--border-strong); color: var(--text-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
table td, .data-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
table tr:hover td { background: var(--surface); }

/* ===== Buttons ===== */
.btn, .ad-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; border: 1px solid transparent; cursor: pointer; transition: var(--transition); }
.btn-primary, .ad-btn-primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.btn-primary:hover { opacity: 0.9; }
.btn-secondary, .ad-btn-secondary { background: var(--surface-2); color: var(--text); border-color: var(--border); }
.btn-ghost, .ad-btn-ghost { background: transparent; color: var(--text-muted); border-color: transparent; }
.btn-ghost:hover { color: var(--text); background: var(--surface); }
.btn-danger { background: var(--danger); color: #fff; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-lg { padding: 12px 24px; font-size: 15px; }

/* ===== Badges & Tags ===== */
.badge, .ad-badge, .tag { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: var(--radius-pill); font-size: 11px; font-weight: 600; }
.badge-success, .ad-badge-success { background: var(--success-dim); color: var(--success); }
.badge-warning, .ad-badge-warning { background: var(--warning-dim); color: var(--warning); }
.badge-danger, .ad-badge-danger { background: var(--danger-dim); color: var(--danger); }
.badge-info { background: var(--info-dim); color: var(--info); }
.badge-neutral { background: var(--surface-3); color: var(--text-muted); }

/* ===== Modals & Overlays ===== */
.modal, .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-content, .modal-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 24px; max-width: 560px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: var(--shadow-lg); }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.modal-close { background: none; border: none; color: var(--text-dim); font-size: 20px; cursor: pointer; }

/* ===== Forms ===== */
input, select, textarea { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 12px; color: var(--text); font-size: 14px; font-family: var(--font); width: 100%; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-dim); }
label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 4px; color: var(--text-muted); }

/* ===== Search Box ===== */
.search-box { position: relative; }
.search-box input { padding-left: 36px; }
.search-ico { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text-dim); }
.search-box kbd { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); font-size: 11px; color: var(--text-dim); background: var(--surface-3); padding: 2px 6px; border-radius: 4px; }

/* ===== Page Headers ===== */
.page-head { margin-bottom: 24px; }
.page-head h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
.page-head .lede, .page-head p { font-size: 14px; color: var(--text-muted); margin-top: 4px; max-width: 600px; }
.eyebrow { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--accent); font-weight: 600; margin-bottom: 4px; }
.crumb { display: flex; gap: 6px; font-size: 13px; color: var(--text-muted); }
.crumb-sep { color: var(--text-dim); }

/* ===== Tabs / Switch ===== */
.tab-bar, .switch-bar { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.tab-item, .switch-opt { padding: 8px 16px; font-size: 13px; color: var(--text-muted); border-bottom: 2px solid transparent; cursor: pointer; transition: var(--transition); }
.tab-item.active, .switch-opt.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

/* ===== Progress Bars ===== */
.progress-bar { height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
.progress-bar .fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width 0.3s; }
.progress-bar .fill.success { background: var(--success); }
.progress-bar .fill.warning { background: var(--warning); }
.progress-bar .fill.danger { background: var(--danger); }

/* ===== Screen section wrapper ===== */
.screen { margin-bottom: 48px; padding: 0; background: transparent; }
.screen-label { font-size: 13px; color: var(--text-dim); padding: 8px 0 16px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }

/* ===== Activity / Timeline ===== */
.activity-item, .timeline-item { display: flex; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.activity-dot, .act-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }

/* ===== Misc Utility ===== */
.text-muted { color: var(--text-muted); }
.text-dim { color: var(--text-dim); }
.text-sm { font-size: 12px; }
.text-lg { font-size: 18px; }
.text-xl { font-size: 24px; }
.font-mono { font-family: var(--font-mono); }
.font-bold { font-weight: 700; }
.mt-1 { margin-top: 8px; } .mt-2 { margin-top: 16px; } .mt-3 { margin-top: 24px; }
.mb-1 { margin-bottom: 8px; } .mb-2 { margin-bottom: 16px; } .mb-3 { margin-bottom: 24px; }
.p-1 { padding: 8px; } .p-2 { padding: 16px; } .p-3 { padding: 24px; }
.gap-1 { gap: 8px; } .gap-2 { gap: 16px; } .gap-3 { gap: 24px; }
.flex { display: flex; }
.flex-col { flex-direction: column; }
.items-center { align-items: center; }
.justify-between { justify-content: space-between; }
.flex-1 { flex: 1; }
.w-full { width: 100%; }
.hidden { display: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--danger); }
.divider, .topbar-divider { width: 1px; height: 24px; background: var(--border); margin: 0 12px; }
"""

# ---------------------------------------------------------------------------
# 반응형 breakpoints
# ---------------------------------------------------------------------------

_RESPONSIVE_BREAKPOINTS = """
/* ===== Responsive ===== */
@media (max-width: 1024px) {
  .admin-nav, .admin-sidebar { width: 220px; }
  .admin-main, .admin-content { padding: 20px; }
  .grid-4 { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .admin-layout, .admin-shell { flex-direction: column; }
  .admin-nav, .admin-sidebar { width: 100%; flex-direction: row; overflow-x: auto; padding: 8px; border-right: none; border-bottom: 1px solid var(--border); }
  .nav-item, .ad-nav-item { white-space: nowrap; }
  .admin-main, .admin-content { padding: 16px; }
  .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  .modal-content, .modal-card { max-width: 95%; }
  .page-head h1 { font-size: 20px; }
  table { font-size: 12px; }
}
@media (max-width: 375px) {
  .admin-main, .admin-content { padding: 12px; }
  .card, .kpi-card { padding: 12px; }
  .kpi-card .value, .stat-value { font-size: 22px; }
}
"""
