"""
Codegen — Frontend infrastructure: design tokens, globals.css, layout, React components.

Extracted from engine/skills/executor.py (Phase 2).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _design_tokens_to_css(tokens: dict) -> str:
    """디자인 토큰 JSON → CSS :root 변수 블록으로 변환."""
    lines = [":root {"]

    # 컬러
    colors = tokens.get("colors", {})
    if colors:
        lines.append("  /* Colors */")
        for key, val in colors.items():
            css_key = key.replace("_", "-")
            lines.append(f"  --{css_key}: {val};")

    # 타이포그래피
    typo = tokens.get("typography", {})
    if typo:
        lines.append("  /* Typography */")
        if typo.get("font_family"):
            lines.append(f"  --font-display: '{typo['font_family']}', sans-serif;")
            lines.append(f"  --font-body: '{typo['font_family']}', sans-serif;")
        if typo.get("body_size"):
            lines.append(f"  --text-base: {typo['body_size']};")
        if typo.get("line_height"):
            lines.append(f"  --line-height: {typo['line_height']};")

    # 간격
    spacing = tokens.get("spacing", {})
    if spacing:
        lines.append("  /* Spacing */")
        for key, val in spacing.items():
            lines.append(f"  --space-{key}: {val};")

    # 효과
    effects = tokens.get("effects", {})
    if effects:
        lines.append("  /* Effects */")
        for key, val in effects.items():
            css_key = key.replace("_", "-")
            lines.append(f"  --{css_key}: {val};")

    # 브레이크포인트
    breakpoints = tokens.get("breakpoints", {})
    if breakpoints:
        lines.append("  /* Breakpoints */")
        for key, val in breakpoints.items():
            lines.append(f"  --bp-{key}: {val};")

    lines.append("}")

    # 테마 정보
    meta = tokens.get("meta", {})
    if meta.get("theme") == "dark":
        lines.insert(0, "/* DARK THEME — 아래 컬러를 그대로 사용할 것 */")

    return "\n".join(lines)


async def _build_frontend_infra_code(
    db: Any,
    project_id: str,
    platform: dict,
) -> dict[str, str] | None:
    """디자인 토큰 + 컴포넌트 라이브러리 → globals.css + layout.tsx + React 컴포넌트 (AI 0회).

    Returns:
        {"globals-css": "...", "layout": "...", "component-Button": "...", ...} or None
    """
    # ── 디자인 토큰 로드 ──
    token_row = await db.fetchone(
        """SELECT av.storage_path AS content FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.name LIKE '%디자인 토큰%'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           LIMIT 1""",
        (project_id,),
    )

    tokens = {}
    if token_row and token_row["content"]:
        try:
            tokens = json.loads(token_row["content"]) if isinstance(token_row["content"], str) else token_row["content"]
        except (ValueError, TypeError):
            pass

    if not tokens:
        logger.warning("frontend_infra_gen_skip project=%s — 디자인 토큰 없음", project_id)
        return None

    # ── 컴포넌트 라이브러리(HTML) 로드 — 분할 노드 포함 ──
    lib_rows = await db.fetchall(
        """SELECT av.storage_path AS content FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ?
             AND (n.name LIKE '%컴포넌트 정의서%'
                  OR n.name = '컴포넌트 라이브러리'
                  OR n.name LIKE '컴포넌트 라이브러리 (%')
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version""",
        (project_id,),
    )
    # 여러 분할 artifact의 JSON 배열을 합치고, 마크다운도 이어붙이기
    lib_content = ""
    _all_components_json: list = []
    for lib_row in (lib_rows or []):
        raw = lib_row.get("content", "")
        if not raw or not isinstance(raw, str):
            continue
        try:
            import json as _json_fi
            parsed = _json_fi.loads(raw)
            if isinstance(parsed, list):
                _all_components_json.extend(parsed)
                continue
        except Exception:
            pass
        # 마크다운 폴백: 이어붙이기
        lib_content += "\n" + raw if lib_content else raw
    # JSON 배열이 합쳐졌으면 직렬화
    if _all_components_json:
        import json as _json_fi
        lib_content = _json_fi.dumps(_all_components_json, ensure_ascii=False)

    result: dict[str, str] = {}

    # ── 1. globals.css (디자인 토큰 → CSS 변수) ──
    token_css = _design_tokens_to_css(tokens)
    result["globals-css"] = _generate_globals_css(tokens, token_css)

    # ── 2. layout.tsx (루트 레이아웃) ──
    result["layout"] = _generate_root_layout(tokens, platform)

    # ── 3. 컴포넌트 라이브러리 HTML → React 변환 ──
    if lib_content:
        components = _extract_components_from_library(lib_content)
        for comp_name, comp_html in components.items():
            react_code = _html_component_to_react(comp_name, comp_html)
            result[f"component-{comp_name}"] = react_code

    # ── 4. 공통 인프라 컴포넌트 (항상 생성) ──
    result["component-LoadingIndicator"] = _generate_loading_component()
    result["component-ErrorBoundary"] = _generate_error_boundary_component()
    result["component-EmptyState"] = _generate_empty_state_component()
    result["component-ToastContainer"] = _generate_toast_component()

    # ── 5. 인터랙션 프리미티브 (항상 생성 — DESIGN에서 누락돼도 BUILD 안전망) ──
    result["component-Modal"] = _generate_modal_component()
    result["component-ConfirmDialog"] = _generate_confirm_dialog_component()
    result["component-BottomSheet"] = _generate_bottom_sheet_component()

    logger.info(
        "frontend_infra_code_built project=%s tokens=%s components=%d",
        project_id, bool(tokens), len([k for k in result if k.startswith("component-")]),
    )
    return result


def _generate_globals_css(tokens: dict, token_css: str) -> str:
    """디자인 토큰 → globals.css 전체."""
    L = []
    L.append("/* FILE: src/app/globals.css */")
    L.append("/* GENERATED BY: programmatic-assembler (AI 0 calls) */")
    L.append("")
    L.append("@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700&display=swap');")

    font = tokens.get("typography", {}).get("font_family", "")
    if font and font != "Noto Sans KR":
        safe_font = font.replace(" ", "+")
        L.append(f"@import url('https://fonts.googleapis.com/css2?family={safe_font}:wght@300;400;500;600;700&display=swap');")
    L.append("")

    # CSS 변수
    L.append(token_css)
    L.append("")

    # 글로벌 리셋
    L.append("/* Global Reset */")
    L.append("*, *::before, *::after {")
    L.append("  box-sizing: border-box;")
    L.append("  margin: 0;")
    L.append("  padding: 0;")
    L.append("}")
    L.append("")
    L.append("html {")
    L.append("  font-size: var(--text-base, 16px);")
    L.append("  line-height: var(--line-height, 1.6);")
    L.append("  -webkit-font-smoothing: antialiased;")
    L.append("}")
    L.append("")
    L.append("body {")
    L.append("  font-family: var(--font-body, 'Noto Sans KR', sans-serif);")
    bg = tokens.get("colors", {}).get("background", tokens.get("colors", {}).get("bg", "#ffffff"))
    fg = tokens.get("colors", {}).get("text", tokens.get("colors", {}).get("foreground", "#111111"))
    L.append(f"  background: {bg};")
    L.append(f"  color: {fg};")
    L.append("  min-height: 100vh;")
    L.append("}")
    L.append("")

    # 유틸리티: 모달/토스트
    L.append("/* Modal Overlay */")
    L.append(".modal-overlay {")
    L.append("  position: fixed; inset: 0;")
    L.append("  background: rgba(0,0,0,0.5);")
    L.append("  display: flex; align-items: center; justify-content: center;")
    L.append("  z-index: 50;")
    L.append("}")
    L.append(".modal-content {")
    L.append("  background: var(--surface, #fff);")
    L.append("  border-radius: var(--radius-lg, 12px);")
    L.append("  padding: var(--space-lg, 24px);")
    L.append("  min-width: 400px; max-width: 90vw; max-height: 90vh;")
    L.append("  overflow-y: auto; position: relative;")
    L.append("}")
    L.append(".modal-close {")
    L.append("  position: absolute; top: 12px; right: 12px;")
    L.append("  background: none; border: none; font-size: 24px; cursor: pointer;")
    L.append("}")
    L.append("")
    L.append("/* Toast */")
    L.append(".toast {")
    L.append("  position: fixed; bottom: 24px; right: 24px;")
    L.append("  background: var(--primary, #333); color: #fff;")
    L.append("  padding: 12px 20px; border-radius: var(--radius-md, 8px);")
    L.append("  z-index: 100; animation: toast-in 0.3s ease;")
    L.append("}")
    L.append("@keyframes toast-in { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }")
    L.append("")
    L.append("/* Modal Header */")
    L.append(".modal-header {")
    L.append("  display: flex; align-items: center; justify-content: space-between;")
    L.append("  margin-bottom: var(--space-md, 16px);")
    L.append("}")
    L.append(".modal-title {")
    L.append("  font-size: var(--text-lg, 18px); font-weight: 600; margin: 0;")
    L.append("}")
    L.append(".modal-content--sm { min-width: 320px; max-width: 420px; }")
    L.append(".modal-content--lg { min-width: 600px; max-width: 800px; }")
    L.append("")
    L.append("/* Bottom Sheet */")
    L.append(".bottom-sheet {")
    L.append("  position: fixed; bottom: 0; left: 0; right: 0;")
    L.append("  background: var(--surface, #fff);")
    L.append("  border-radius: var(--radius-lg, 12px) var(--radius-lg, 12px) 0 0;")
    L.append("  padding: var(--space-md, 16px) var(--space-lg, 24px) var(--space-xl, 32px);")
    L.append("  max-height: 85vh; overflow-y: auto;")
    L.append("  animation: sheet-up 0.3s var(--ease-out, ease-out);")
    L.append("}")
    L.append(".bottom-sheet__handle {")
    L.append("  width: 36px; height: 4px; border-radius: 2px;")
    L.append("  background: var(--color-border, #ddd); margin: 0 auto var(--space-md, 16px);")
    L.append("}")
    L.append(".bottom-sheet__title {")
    L.append("  font-size: var(--text-lg, 18px); font-weight: 600;")
    L.append("  margin: 0 0 var(--space-md, 16px);")
    L.append("}")
    L.append("@keyframes sheet-up { from { transform: translateY(100%); } to { transform: translateY(0); } }")
    L.append("")

    # 반응형 breakpoints
    bp = tokens.get("breakpoints", {})
    if bp.get("tablet"):
        L.append(f"@media (max-width: {bp['tablet']}) {{")
        L.append("  html { font-size: 15px; }")
        L.append("}")
    if bp.get("mobile"):
        L.append(f"@media (max-width: {bp['mobile']}) {{")
        L.append("  html { font-size: 14px; }")
        L.append("  .modal-content { min-width: unset; width: 95vw; }")
        L.append("}")

    return "\n".join(L)


def _generate_root_layout(tokens: dict, platform: dict) -> str:
    """Next.js app/layout.tsx 생성."""
    font = tokens.get("typography", {}).get("font_family", "Noto Sans KR")
    meta = tokens.get("meta", {})
    theme = meta.get("theme", "light")

    L = []
    L.append("// FILE: src/app/layout.tsx")
    L.append("// GENERATED BY: programmatic-assembler (AI 0 calls)")
    L.append("import type { Metadata } from 'next';")
    L.append("import './globals.css';")
    L.append("")
    L.append("export const metadata: Metadata = {")
    L.append("  title: '앱',")
    L.append("  description: '프로그래매틱 생성',")
    L.append("};")
    L.append("")
    L.append("export default function RootLayout({ children }: { children: React.ReactNode }) {")
    L.append("  return (")
    L.append(f'    <html lang="ko" data-theme="{theme}">')
    L.append(f"      <body>")
    L.append("        {children}")
    L.append("      </body>")
    L.append("    </html>")
    L.append("  );")
    L.append("}")
    return "\n".join(L)


def _extract_components_from_library(lib_content: str) -> dict[str, str]:
    """컴포넌트 정의서/라이브러리 마크다운에서 컴포넌트 HTML 추출.

    패턴: ### ComponentName 또는 ## Button 다음 ```html 블록
    """
    import re as _re
    components: dict[str, str] = {}

    # 패턴: 헤딩 + html/jsx 코드블록
    for m in _re.finditer(
        r'(?:#{2,4})\s*(\w+)(?:\s*컴포넌트)?\s*\n[\s\S]*?```(?:html|jsx|tsx)\n([\s\S]*?)```',
        lib_content,
    ):
        name = m.group(1).strip()
        html = m.group(2).strip()
        if name and html and len(html) > 20:
            components[name] = html

    return components


def _html_component_to_react(comp_name: str, html: str) -> str:
    """HTML 컴포넌트 조각 → React FC 변환."""
    import re as _re

    # HTML → JSX 변환
    jsx = html
    jsx = jsx.replace("class=", "className=")
    jsx = _re.sub(r'for="([^"]*)"', r'htmlFor="\1"', jsx)
    jsx = _re.sub(r'onclick="[^"]*"', '', jsx)
    jsx = _re.sub(r'style="([^"]*)"', lambda m: _inline_style_to_jsx(m.group(1)), jsx)
    # self-closing tags
    for tag in ("img", "input", "br", "hr"):
        jsx = _re.sub(rf'<{tag}([^>]*)(?<!/)>', rf'<{tag}\1 />', jsx)

    # Props 추출 — HTML에서 동적 바인딩 {{xxx}} 패턴
    prop_names: list[str] = []
    for pm in _re.finditer(r'\{\{(\w+)\}\}', jsx):
        prop_names.append(pm.group(1))
    # {{xxx}} → {xxx}
    jsx = _re.sub(r'\{\{(\w+)\}\}', r'{\1}', jsx)

    # 이벤트 prop 추정
    comp_lower = comp_name.lower()
    if "button" in comp_lower or "btn" in comp_lower:
        if "onClick" not in " ".join(prop_names):
            prop_names.append("onClick")
    if "form" in comp_lower:
        if "onSubmit" not in " ".join(prop_names):
            prop_names.append("onSubmit")

    # 기본 props: children
    has_children = "</" in html and "{{children}}" not in html

    props_interface = []
    for p in sorted(set(prop_names)):
        if p.startswith("on"):
            props_interface.append(f"  {p}?: () => void;")
        else:
            props_interface.append(f"  {p}?: string;")
    if has_children:
        props_interface.append("  children?: React.ReactNode;")

    L = []
    L.append(f"// FILE: src/components/{comp_name}.tsx")
    L.append("// GENERATED BY: programmatic-assembler (AI 0 calls)")
    L.append("import React from 'react';")
    L.append("")
    L.append(f"interface {comp_name}Props {{")
    for pi in props_interface:
        L.append(pi)
    L.append("}")
    L.append("")

    all_props = sorted(set(prop_names))
    if has_children:
        all_props.append("children")
    destructure = ", ".join(all_props) if all_props else ""

    L.append(f"export default function {comp_name}({{ {destructure} }}: {comp_name}Props) {{")
    L.append("  return (")
    # 들여쓰기 적용
    for line in jsx.split("\n"):
        L.append(f"    {line}")
    L.append("  );")
    L.append("}")
    return "\n".join(L)


def _inline_style_to_jsx(style_str: str) -> str:
    """CSS inline style → JSX style object."""
    import re as _re
    parts = []
    for decl in style_str.split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        # kebab → camelCase
        camel = _re.sub(r'-(\w)', lambda m: m.group(1).upper(), prop.strip())
        parts.append(f"{camel}: '{val.strip()}'")
    return "style={{" + ", ".join(parts) + "}}" if parts else ""


def _generate_loading_component() -> str:
    return """// FILE: src/components/LoadingIndicator.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
import React from 'react';

export default function LoadingIndicator() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '200px' }}>
      <div style={{
        width: 40, height: 40, border: '3px solid var(--border, #eee)',
        borderTopColor: 'var(--primary, #333)', borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
      }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
"""


def _generate_error_boundary_component() -> str:
    return """// FILE: src/components/ErrorBoundary.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
import React from 'react';

interface ErrorBoundaryProps {
  error: Error;
  onRetry?: () => void;
}

export default function ErrorBoundary({ error, onRetry }: ErrorBoundaryProps) {
  return (
    <div style={{ padding: 40, textAlign: 'center' }}>
      <h2 style={{ color: 'var(--error, #dc2626)', marginBottom: 8 }}>오류가 발생했습니다</h2>
      <p style={{ color: 'var(--text-muted, #666)', marginBottom: 16 }}>{error.message}</p>
      {onRetry && (
        <button onClick={onRetry} style={{
          padding: '8px 20px', background: 'var(--primary, #333)', color: '#fff',
          border: 'none', borderRadius: 'var(--radius-md, 8px)', cursor: 'pointer',
        }}>
          다시 시도
        </button>
      )}
    </div>
  );
}
"""


def _generate_empty_state_component() -> str:
    return """// FILE: src/components/EmptyState.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
import React from 'react';

interface EmptyStateProps {
  message?: string;
  onAction?: () => void;
  actionLabel?: string;
}

export default function EmptyState({ message = '데이터가 없습니다', onAction, actionLabel = '새로고침' }: EmptyStateProps) {
  return (
    <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-muted, #999)' }}>
      <p style={{ fontSize: '1.1em', marginBottom: 16 }}>{message}</p>
      {onAction && (
        <button onClick={onAction} style={{
          padding: '8px 20px', background: 'var(--primary, #333)', color: '#fff',
          border: 'none', borderRadius: 'var(--radius-md, 8px)', cursor: 'pointer',
        }}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
"""


def _generate_modal_component() -> str:
    return """// FILE: src/components/Modal.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
'use client';
import React, { useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  size?: 'sm' | 'md' | 'lg';
}

export default function Modal({ isOpen, onClose, title, children, size = 'md' }: ModalProps) {
  const contentRef = useRef<HTMLDivElement>(null);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') onClose();
  }, [onClose]);

  useEffect(() => {
    if (!isOpen) return;
    document.addEventListener('keydown', handleKeyDown);
    document.body.style.overflow = 'hidden';
    contentRef.current?.focus();
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = '';
    };
  }, [isOpen, handleKeyDown]);

  if (!isOpen) return null;

  const sizeClass = size === 'sm' ? 'modal-content--sm' : size === 'lg' ? 'modal-content--lg' : '';

  return createPortal(
    <div className="modal-overlay" onClick={onClose} role="presentation">
      <div
        ref={contentRef}
        className={`modal-content ${sizeClass}`}
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title || '다이얼로그'}
        tabIndex={-1}
      >
        {title && (
          <div className="modal-header">
            <h2 className="modal-title">{title}</h2>
            <button className="modal-close" onClick={onClose} aria-label="닫기">&times;</button>
          </div>
        )}
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function useModal() {
  const [isOpen, setIsOpen] = React.useState(false);
  return { isOpen, open: () => setIsOpen(true), close: () => setIsOpen(false) };
}
"""


def _generate_confirm_dialog_component() -> str:
    return """// FILE: src/components/ConfirmDialog.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
'use client';
import React from 'react';
import Modal from './Modal';

interface ConfirmDialogProps {
  isOpen: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

export default function ConfirmDialog({
  isOpen, onConfirm, onCancel, title, message,
  confirmText = '확인', cancelText = '취소', danger = false,
}: ConfirmDialogProps) {
  return (
    <Modal isOpen={isOpen} onClose={onCancel} title={title} size="sm">
      <p style={{ margin: 'var(--space-md, 16px) 0', color: 'var(--color-text-secondary, #666)' }}>
        {message}
      </p>
      <div style={{ display: 'flex', gap: 'var(--space-sm, 8px)', justifyContent: 'flex-end' }}>
        <button
          onClick={onCancel}
          style={{
            padding: '8px 16px', border: '1px solid var(--color-border, #ddd)',
            borderRadius: 'var(--radius-md, 8px)', background: 'transparent',
            cursor: 'pointer', color: 'var(--color-text-primary, #333)',
          }}
        >
          {cancelText}
        </button>
        <button
          onClick={onConfirm}
          style={{
            padding: '8px 16px', border: 'none',
            borderRadius: 'var(--radius-md, 8px)', cursor: 'pointer', color: '#fff',
            background: danger ? 'var(--color-danger, #ef4444)' : 'var(--color-primary, #3b82f6)',
          }}
        >
          {confirmText}
        </button>
      </div>
    </Modal>
  );
}
"""


def _generate_bottom_sheet_component() -> str:
    return """// FILE: src/components/BottomSheet.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
'use client';
import React, { useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';

interface BottomSheetProps {
  isOpen: boolean;
  onClose: () => void;
  children: React.ReactNode;
  title?: string;
}

export default function BottomSheet({ isOpen, onClose, children, title }: BottomSheetProps) {
  const sheetRef = useRef<HTMLDivElement>(null);
  const startY = useRef(0);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    startY.current = e.touches[0].clientY;
  }, []);

  const handleTouchEnd = useCallback((e: React.TouchEvent) => {
    const diff = e.changedTouches[0].clientY - startY.current;
    if (diff > 100) onClose();
  }, [onClose]);

  useEffect(() => {
    if (!isOpen) return;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, [isOpen]);

  if (!isOpen) return null;

  /* Desktop (768px+): modal 스타일로 폴백 */
  const isDesktop = typeof window !== 'undefined' && window.innerWidth >= 768;

  if (isDesktop) {
    return createPortal(
      <div className="modal-overlay" onClick={onClose} role="presentation">
        <div className="modal-content" onClick={e => e.stopPropagation()}
          role="dialog" aria-modal="true" aria-label={title || '시트'}>
          {title && <div className="modal-header"><h2 className="modal-title">{title}</h2>
            <button className="modal-close" onClick={onClose} aria-label="닫기">&times;</button></div>}
          <div className="modal-body">{children}</div>
        </div>
      </div>, document.body,
    );
  }

  return createPortal(
    <div className="modal-overlay" onClick={onClose} role="presentation">
      <div
        ref={sheetRef}
        className="bottom-sheet"
        onClick={e => e.stopPropagation()}
        onTouchStart={handleTouchStart}
        onTouchEnd={handleTouchEnd}
        role="dialog"
        aria-modal="true"
        aria-label={title || '바텀 시트'}
      >
        <div className="bottom-sheet__handle" aria-hidden="true" />
        {title && <h2 className="bottom-sheet__title">{title}</h2>}
        <div className="bottom-sheet__body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
"""


def _generate_toast_component() -> str:
    return """// FILE: src/components/ToastContainer.tsx
// GENERATED BY: programmatic-assembler (AI 0 calls)
import React, { useEffect } from 'react';

interface ToastContainerProps {
  message: string | null;
  onClose: () => void;
  duration?: number;
}

export default function ToastContainer({ message, onClose, duration = 3000 }: ToastContainerProps) {
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(onClose, duration);
    return () => clearTimeout(timer);
  }, [message, onClose, duration]);

  if (!message) return null;

  return (
    <div className="toast" onClick={onClose}>
      {message}
    </div>
  );
}
"""
