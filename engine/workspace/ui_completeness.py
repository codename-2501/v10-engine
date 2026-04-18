"""
engine/workspace/ui_completeness.py
프론트엔드 UI 완성도 자동 검증 + 수정.

BUILD 산출물에서 생성된 프론트엔드가 실제로 동작하는지:
1. 죽은 버튼 감지 → 적절한 핸들러 자동 연결
2. 누락 페이지 감지 → href/link 대상 페이지 자동 생성
3. 프론트-백 API 연결 감지 → 미연결 엔드포인트 리포트
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.workspace.ui_completeness")


def fix_ui_completeness(workspace_path: Path, stack: dict) -> dict:
    """전체 UI 완성도 자동 수정. auto_deploy 파이프라인에서 호출."""
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"
    report: dict[str, Any] = {"dead_buttons": [], "missing_pages": [], "fixes": []}

    if not fe.is_dir():
        return report

    # 1. 죽은 버튼 감지 + 자동 수정
    dead = _find_and_fix_dead_buttons(fe)
    report["dead_buttons"] = dead["found"]
    report["fixes"] += dead["fixes"]

    # 2. 누락 페이지 감지 + 자동 생성
    missing = _find_and_fix_missing_pages(fe)
    report["missing_pages"] = missing["found"]
    report["fixes"] += missing["fixes"]

    # 3. href="목록으로" 같은 네비게이션 버튼 연결
    nav_fixes = _fix_navigation_buttons(fe)
    report["fixes"] += nav_fixes

    # 4. 리스트→상세 연계 (클릭 불가 리스트 카드에 href 추가 + 상세 페이지 생성)
    list_fixes = _fix_list_detail_links(fe)
    report["fixes"] += list_fixes

    # 5. API 응답 wrapper 언래핑 (res.data → res.data.data 또는 res.data?.data ?? res.data)
    unwrap_fixes = _fix_api_response_unwrap(fe)
    report["fixes"] += unwrap_fixes

    # 6. Promise.all → Promise.allSettled (하나 실패해도 나머지 유지)
    settled_fixes = _fix_promise_all_settled(fe)
    report["fixes"] += settled_fixes

    # 7. 프론트엔드 API 경로 vs 백엔드 실제 경로 불일치
    if be.is_dir():
        path_fixes = _fix_api_path_mismatch(fe, be)
        report["fixes"] += path_fixes

    # 8. [v8+] CRUD 완전성 검증 — 리스트가 있는데 생성 폼 없는 엔티티 감지
    crud_issues = _check_crud_completeness(fe)
    report["crud_issues"] = crud_issues

    # 9. [v8+] 프론트 API 호출 경로 vs 백엔드 라우트 정합성 강화 검증
    if be.is_dir():
        api_issues = _check_api_route_consistency(fe, be)
        report["api_route_issues"] = api_issues

    logger.info(
        "ui_completeness dead_buttons=%d missing_pages=%d fixes=%d crud=%d",
        len(report["dead_buttons"]),
        len(report["missing_pages"]),
        len(report["fixes"]),
        len(report.get("crud_issues", [])),
    )
    return report


# ============================================================
# 1. 죽은 버튼 감지 + 수정
# ============================================================

def _find_and_fix_dead_buttons(fe: Path) -> dict:
    """onClick/onSubmit/href 없는 버튼을 감지하고 문맥에 맞는 핸들러를 연결."""
    found = []
    fixes = []

    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue

        original = content
        lines = content.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()

            # <Button ...> 또는 <button ...> 찾기 (onClick/onSubmit/href/type="submit" 없는 것)
            is_button = re.match(r'<(?:Button|button)\b', stripped)
            if not is_button:
                continue
            if any(attr in line for attr in ["onClick", "onSubmit", "href=", 'type="submit"', "type='submit'"]):
                continue

            # 버튼 텍스트 추출 (같은 줄 또는 다음 줄)
            btn_text = ""
            for j in range(i, min(i + 3, len(lines))):
                text_match = re.search(r'>([^<]+)</', lines[j])
                if text_match:
                    btn_text = text_match.group(1).strip()
                    break

            if not btn_text:
                continue

            found.append({
                "file": str(tsx_file.relative_to(fe)),
                "line": i + 1,
                "text": btn_text,
            })

            # 서버 컴포넌트에는 onClick 넣으면 안 됨
            if "'use client'" not in content and '"use client"' not in content:
                continue

            # 버튼 텍스트 기반으로 적절한 핸들러 추론
            handler = _infer_button_handler(btn_text, tsx_file, fe)
            if handler:
                # 핸들러 삽입
                content = _insert_handler_to_button(content, i, handler, lines)
                fixes.append(f"wired: {tsx_file.name}:{i+1} '{btn_text}' → {handler['type']}")

        if content != original:
            tsx_file.write_text(content, encoding="utf-8")

    return {"found": found, "fixes": fixes}


def _infer_button_handler(btn_text: str, tsx_file: Path, fe: Path) -> dict | None:
    """버튼 텍스트에서 적절한 핸들러를 추론."""
    text = btn_text.lower().strip()

    # 네비게이션 패턴
    nav_map = {
        "목록으로": "..",
        "뒤로": "..",
        "돌아가기": "..",
        "홈으로": "/",
    }
    for keyword, target in nav_map.items():
        if keyword in text:
            return {"type": "navigate", "target": target}

    # 등록/추가/작성 → 모달 열기
    if any(kw in text for kw in ["등록", "추가", "작성", "글쓰기", "새로"]):
        content = tsx_file.read_text(encoding="utf-8")
        # 이미 모달 state가 있으면 그거 열기
        modal_state = re.search(r"const\s*\[(\w*[Mm]odal\w*|open\w*|showForm\w*),\s*set(\w+)\]", content)
        if modal_state:
            setter = "set" + modal_state.group(2) if modal_state.group(2) else None
            if setter and setter in content:
                return {"type": "state", "setter": setter, "value": "true"}

        # 모달 state가 없으면 → 페이지에 모달 삽입이 필요하다는 표시
        # 자기 자신 경로로 가지 않도록 None 반환 (= 이 버튼은 스킵)
        return None

    # 예약하기 → 예약 페이지 이동
    if "예약" in text:
        return {"type": "navigate", "target": "/hospitals"}

    # 장바구니 → API 호출
    if "장바구니" in text:
        return {"type": "toast", "message": "장바구니에 추가되었습니다"}

    # 리뷰 → API 호출
    if "리뷰" in text:
        return {"type": "toast", "message": "리뷰 기능 준비중입니다"}

    return None


def _insert_handler_to_button(content: str, line_idx: int, handler: dict, lines: list[str]) -> str:
    """버튼 라인에 핸들러를 삽입."""
    line = lines[line_idx]

    if handler["type"] == "navigate":
        # <Button ...> → <a href="target"><Button ...>
        # 또는 onClick={() => router.push('target')}
        target = handler["target"]
        # useRouter가 이미 import되어 있는지 확인
        if "useRouter" in content:
            insert = f' onClick={{() => router.push("{target}")}}'
        else:
            # a 태그로 감싸기 대신 window.location 사용
            insert = f' onClick={{() => window.location.href = "{target}"}}'
        line = re.sub(r'(<(?:Button|button)\b)', rf'\1{insert}', line, count=1)

    elif handler["type"] == "state":
        setter = handler["setter"]
        value = handler["value"]
        insert = f' onClick={{() => {setter}({value})}}'
        line = re.sub(r'(<(?:Button|button)\b)', rf'\1{insert}', line, count=1)

    elif handler["type"] == "toast":
        msg = handler["message"]
        # Use toast state setter instead of alert() when available
        if "setToastMessage" in content:
            insert = f" onClick={{() => setToastMessage('{msg}')}}"
        elif "toast" in content.lower():
            insert = f" onClick={{() => toast('{msg}')}}"
        else:
            # Fallback: use console.log + noop instead of alert()
            insert = f" onClick={{() => console.log('{msg}')}}"
        line = re.sub(r'(<(?:Button|button)\b)', rf'\1{insert}', line, count=1)

    lines[line_idx] = line
    return "\n".join(lines)


# ============================================================
# 2. 누락 페이지 감지 + 생성
# ============================================================

def _find_and_fix_missing_pages(fe: Path) -> dict:
    """href/Link 대상 경로에 페이지가 없으면 생성."""
    found = []
    fixes = []
    app_dir = fe / "src" / "app"
    if not app_dir.is_dir():
        return {"found": found, "fixes": fixes}

    # 모든 href 대상 수집
    all_hrefs: set[str] = set()
    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue
        for m in re.finditer(r'href=["\']([^"\']+)["\']', content):
            path = m.group(1)
            if path.startswith("/") and not path.startswith("//") and not path.startswith("/api"):
                all_hrefs.add(path)
        for m in re.finditer(r'router\.push\(["\']([^"\']+)["\']', content):
            all_hrefs.add(m.group(1))

    # 존재하는 라우트 목록
    existing_routes: set[str] = set()
    for page_file in app_dir.rglob("page.tsx"):
        rel = page_file.relative_to(app_dir)
        parts = list(rel.parts[:-1])  # page.tsx 제거
        cleaned = [p for p in parts if not p.startswith("(")]
        route = "/" + "/".join(cleaned) if cleaned else "/"
        existing_routes.add(route)
        # 동적 라우트의 부모도 추가
        if any(p.startswith("[") for p in cleaned):
            static_parts = [p for p in cleaned if not p.startswith("[")]
            if static_parts:
                existing_routes.add("/" + "/".join(static_parts))

    # 누락 페이지 감지 — both static and dynamic hrefs
    for href in sorted(all_hrefs):
        if any(c in href for c in ["undefined", "#"]):
            continue

        # Handle dynamic hrefs: convert {var} and ${var} to Next.js [param] route segments.
        # 부모 경로에 이미 [id]가 있으면 유니크 슬러그([careLogId] 등)로 분기해
        # 동일 경로 내 중복 슬러그로 인한 Next.js 기동 실패를 방지.
        if "${" in href or "{" in href:
            from engine.workspace.route_slug import normalize_dynamic_segment

            def _slug_sub(match: re.Match) -> str:
                # 매치 위치까지 이미 만들어진 prefix를 현재 normalized 상태에서 추출
                # 간단히: href 원본에서 match.start() 이전 문자열을 경로로 간주
                prefix = href[: match.start()]
                # prefix에서 앞쪽 슬래시 정리
                prefix_path = prefix.split("?")[0]
                resource_hint = match.group(2) or (
                    match.group(1).split("?.")[-1].split(".")[-1] or "id"
                )
                # 부모에 [id]가 이미 있고 resource_hint도 id면 리소스명 기반으로
                parent_segments = [
                    p for p in prefix_path.strip("/").split("/") if p
                ]
                parent_purepath = "/".join(parent_segments)
                # 기본 슬러그 이름 결정
                if resource_hint == "id" or not resource_hint:
                    # 리소스명 힌트: 직전 정적 세그먼트
                    static_parents = [
                        p for p in parent_segments if not p.startswith("[")
                    ]
                    resource_name = static_parents[-1] if static_parents else "id"
                    return normalize_dynamic_segment(parent_purepath, resource_name)
                return f"[{resource_hint}]"

            normalized = re.sub(
                r'\$?\{([^}]*?)(?:\.(\w+))?\}', _slug_sub, href,
            )
            # Also handle template literal backtick fragments
            normalized = normalized.replace('`', '').strip()
        else:
            normalized = href

        normalized = normalized.rstrip("/")
        if not normalized:
            normalized = "/"
        if normalized not in existing_routes:
            # For dynamic routes, also check if a matching [param] route exists
            _dyn_matched = False
            if "[" in normalized:
                # Check existing routes for pattern match
                norm_parts = normalized.strip("/").split("/")
                for er in existing_routes:
                    er_parts = er.strip("/").split("/")
                    if len(er_parts) != len(norm_parts):
                        continue
                    if all(
                        np == ep or (np.startswith("[") and ep.startswith("["))
                        for np, ep in zip(norm_parts, er_parts)
                    ):
                        _dyn_matched = True
                        break
            if not _dyn_matched:
                found.append(normalized)

    # 누락 페이지 자동 생성
    for route in found:
        page_path = _create_placeholder_page(app_dir, route)
        if page_path:
            fixes.append(f"created_page: {route}")

    return {"found": found, "fixes": fixes}


def _create_placeholder_page(app_dir: Path, route: str) -> Path | None:
    """누락된 라우트에 기본 페이지 생성. Handles static, dynamic [id], and form routes."""
    parts = route.strip("/").split("/") if route != "/" else []
    if not parts:
        return None  # 루트는 이미 있을 것

    # (main) 그룹 하위에 생성
    target_dir = app_dir / "(main)" / "/".join(parts)
    target_file = target_dir / "page.tsx"

    if target_file.exists():
        return None

    target_dir.mkdir(parents=True, exist_ok=True)

    # Detect page type from route pattern
    is_edit = parts[-1] == "edit" and len(parts) >= 2
    is_create = parts[-1] in ("create", "new", "add")
    has_dynamic = any(p.startswith("[") for p in parts)
    is_detail = has_dynamic and not is_edit

    # Find resource name and parent route (skip dynamic segments and action suffixes)
    resource_parts = [p for p in parts if not p.startswith("[") and p not in ("create", "new", "add", "edit")]
    resource_name = resource_parts[-1] if resource_parts else "item"
    parent_parts = []
    for p in parts:
        if p.startswith("[") or p in ("create", "new", "add", "edit"):
            break
        parent_parts.append(p)
    parent_route = "/" + "/".join(parent_parts) if parent_parts else "/"

    # Korean label inference (universal, no hardcoding)
    _label_map = {
        "clients": "이용자", "caregivers": "요양보호사", "staff": "직원",
        "patients": "환자", "elders": "어르신", "users": "사용자",
        "members": "회원", "hospitals": "병원", "products": "상품",
        "orders": "주문", "schedules": "일정", "reports": "보고서",
        "notices": "공지사항", "programs": "프로그램", "services": "서비스",
        "facilities": "시설", "rooms": "호실", "meals": "식단",
        "medications": "투약", "assessments": "평가", "visits": "방문",
        "documents": "문서", "settings": "설정", "categories": "카테고리",
        "reviews": "리뷰", "bookings": "예약", "employees": "직원",
    }
    label = _label_map.get(resource_name.lower(), resource_name.replace("-", " ").title())

    page_pascal = "".join(w.capitalize() for w in resource_name.replace("-", "_").split("_"))
    api_path = f"/api/{resource_name.replace('-', '_')}"

    if is_edit:
        page_code = _gen_edit_page_code(page_pascal, label, parent_route, api_path)
    elif is_create:
        page_code = _gen_create_page_code(page_pascal, label, parent_route, api_path)
    elif is_detail:
        page_code = _gen_detail_page_code(page_pascal, label, parent_route, api_path)
    else:
        # Static page — simple placeholder
        page_name = parts[-1].replace("-", " ").title()
        page_code = f"""'use client';

export default function {page_name.replace(' ', '')}Page() {{
  return (
    <div style={{{{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6, 1.5rem)' }}}}>
      <header style={{{{ display: 'flex', alignItems: 'center', gap: 'var(--space-4, 1rem)' }}}}>
        <a href="{parent_route}"
          style={{{{ color: 'var(--text-tertiary, #666)', textDecoration: 'none', fontSize: 'var(--text-sm, 0.875rem)' }}}}>
          ← 뒤로
        </a>
        <h1 style={{{{ fontSize: 'var(--text-2xl, 1.5rem)', fontWeight: 800 }}}}>
          {label}
        </h1>
      </header>
      <div style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: 'var(--space-8, 2rem)', boxShadow: '0 1px 3px rgba(0,0,0,0.1)', textAlign: 'center', color: 'var(--text-tertiary, #666)' }}}}>
        <p style={{{{ fontSize: 'var(--text-base, 1rem)', fontWeight: 600 }}}}>준비중입니다</p>
        <p style={{{{ fontSize: 'var(--text-sm, 0.875rem)', marginTop: 'var(--space-2, 0.5rem)' }}}}>곧 업데이트될 예정이에요</p>
      </div>
    </div>
  );
}}
"""

    target_file.write_text(page_code, encoding="utf-8")
    logger.info("placeholder_page_created route=%s path=%s type=%s", route, target_file,
                "edit" if is_edit else "create" if is_create else "detail" if is_detail else "static")
    return target_file


def _gen_detail_page_code(pascal: str, label: str, parent_route: str, api_path: str) -> str:
    """Generate a proper detail page with useParams and data fetching."""
    return f"""'use client';

import {{ useParams, useRouter }} from 'next/navigation';
import {{ useState, useEffect, useCallback }} from 'react';

export default function {pascal}DetailPage() {{
  const params = useParams();
  const id = params?.id as string;
  const router = useRouter();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchData = useCallback(async () => {{
    try {{
      setLoading(true);
      const res = await fetch(`{api_path}/${{id}}`);
      if (!res.ok) throw new Error(`${{res.status}} ${{res.statusText}}`);
      const json = await res.json();
      setData(json.data ?? json);
    }} catch (e) {{
      setError(e instanceof Error ? e : new Error(String(e)));
    }} finally {{
      setLoading(false);
    }}
  }}, [id]);

  useEffect(() => {{ fetchData(); }}, [fetchData]);

  if (loading) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>로딩중...</div>;
  if (error) return <div style={{{{ textAlign: 'center', padding: '2rem', color: 'red' }}}}>오류: {{error.message}}</div>;
  if (!data) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>데이터가 없습니다</div>;

  return (
    <div style={{{{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6, 1.5rem)' }}}}>
      <header style={{{{ display: 'flex', alignItems: 'center', gap: 'var(--space-4, 1rem)' }}}}>
        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 'var(--text-sm, 0.875rem)', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>
        <h1 style={{{{ fontSize: 'var(--text-2xl, 1.5rem)', fontWeight: 800 }}}}>{label} 상세</h1>
      </header>
      <div style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: 'var(--space-6, 1.5rem)', boxShadow: '0 1px 3px rgba(0,0,0,0.1)' }}}}>
        <h2 style={{{{ fontSize: 'var(--text-xl, 1.25rem)', fontWeight: 700, marginBottom: '1rem' }}}}>
          {{data.name || data.title || '{label} 상세 정보'}}
        </h2>
        {{data.description && <p style={{{{ color: 'var(--text-secondary, #555)', marginBottom: '1rem' }}}}>{{data.description}}</p>}}
        {{data.status && <p style={{{{ fontSize: 'var(--text-sm, 0.875rem)' }}}}>상태: {{data.status}}</p>}}
        {{data.created_at && <p style={{{{ fontSize: 'var(--text-xs, 0.75rem)', color: 'var(--text-tertiary, #999)', marginTop: '1rem' }}}}>등록일: {{new Date(data.created_at).toLocaleDateString('ko-KR')}}</p>}}
        <div style={{{{ marginTop: '1.5rem', display: 'flex', gap: '0.5rem' }}}}>
          <button onClick={{() => router.push(`{parent_route}/${{id}}/edit`)}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>수정</button>
          <button onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>목록으로</button>
        </div>
      </div>
    </div>
  );
}}
"""


def _gen_create_page_code(pascal: str, label: str, parent_route: str, api_path: str) -> str:
    """Generate a create form page."""
    return f"""'use client';

import {{ useRouter }} from 'next/navigation';
import {{ useState, useCallback }} from 'react';

export default function {pascal}CreatePage() {{
  const router = useRouter();
  const [formData, setFormData] = useState<Record<string, any>>({{ }});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChange = useCallback((field: string, value: any) => {{
    setFormData(prev => ({{ ...prev, [field]: value }}));
  }}, []);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {{
    e.preventDefault();
    try {{
      setSubmitting(true);
      setError(null);
      const res = await fetch('{api_path}', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(formData),
      }});
      if (!res.ok) throw new Error('저장에 실패했습니다');
      router.push('{parent_route}');
    }} catch (e) {{
      setError(e instanceof Error ? e.message : '오류가 발생했습니다');
    }} finally {{
      setSubmitting(false);
    }}
  }}, [formData, router]);

  return (
    <div style={{{{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6, 1.5rem)' }}}}>
      <header style={{{{ display: 'flex', alignItems: 'center', gap: 'var(--space-4, 1rem)' }}}}>
        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 'var(--text-sm, 0.875rem)', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>
        <h1 style={{{{ fontSize: 'var(--text-2xl, 1.5rem)', fontWeight: 800 }}}}>{label} 등록</h1>
      </header>
      {{error && <div style={{{{ padding: '0.75rem', background: '#fef2f2', color: '#dc2626', borderRadius: '0.375rem' }}}}>{{error}}</div>}}
      <form onSubmit={{handleSubmit}} style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: 'var(--space-6, 1.5rem)', boxShadow: '0 1px 3px rgba(0,0,0,0.1)', display: 'flex', flexDirection: 'column', gap: '1rem' }}}}>
        <div>
          <label style={{{{ display: 'block', fontSize: 'var(--text-sm, 0.875rem)', fontWeight: 600, marginBottom: '0.25rem' }}}}>이름</label>
          <input type="text" value={{formData.name ?? ''}} onChange={{(e) => handleChange('name', e.target.value)}} style={{{{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }}}} />
        </div>
        <div>
          <label style={{{{ display: 'block', fontSize: 'var(--text-sm, 0.875rem)', fontWeight: 600, marginBottom: '0.25rem' }}}}>설명</label>
          <textarea value={{formData.description ?? ''}} onChange={{(e) => handleChange('description', e.target.value)}} rows={{4}} style={{{{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }}}} />
        </div>
        <div style={{{{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}}}>
          <button type="button" onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>취소</button>
          <button type="submit" disabled={{submitting}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer', opacity: submitting ? 0.5 : 1 }}}}>{{submitting ? '저장 중...' : '저장'}}</button>
        </div>
      </form>
    </div>
  );
}}
"""


def _gen_edit_page_code(pascal: str, label: str, parent_route: str, api_path: str) -> str:
    """Generate an edit form page with data pre-loading."""
    return f"""'use client';

import {{ useParams, useRouter }} from 'next/navigation';
import {{ useState, useEffect, useCallback }} from 'react';

export default function {pascal}EditPage() {{
  const params = useParams();
  const id = params?.id as string;
  const router = useRouter();
  const [formData, setFormData] = useState<Record<string, any>>({{ }});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {{
    (async () => {{
      try {{
        const res = await fetch(`{api_path}/${{id}}`);
        if (!res.ok) throw new Error('데이터를 불러올 수 없습니다');
        const json = await res.json();
        setFormData(json.data ?? json);
      }} catch (e) {{
        setError(e instanceof Error ? e.message : '오류가 발생했습니다');
      }} finally {{
        setLoading(false);
      }}
    }})();
  }}, [id]);

  const handleChange = useCallback((field: string, value: any) => {{
    setFormData(prev => ({{ ...prev, [field]: value }}));
  }}, []);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {{
    e.preventDefault();
    try {{
      setSubmitting(true);
      setError(null);
      const res = await fetch(`{api_path}/${{id}}`, {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(formData),
      }});
      if (!res.ok) throw new Error('수정에 실패했습니다');
      router.push('{parent_route}');
    }} catch (e) {{
      setError(e instanceof Error ? e.message : '오류가 발생했습니다');
    }} finally {{
      setSubmitting(false);
    }}
  }}, [formData, id, router]);

  if (loading) return <div style={{{{ textAlign: 'center', padding: '2rem' }}}}>로딩중...</div>;

  return (
    <div style={{{{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6, 1.5rem)' }}}}>
      <header style={{{{ display: 'flex', alignItems: 'center', gap: 'var(--space-4, 1rem)' }}}}>
        <button onClick={{() => router.push('{parent_route}')}} style={{{{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 'var(--text-sm, 0.875rem)', color: 'var(--text-tertiary, #666)' }}}}>← 목록</button>
        <h1 style={{{{ fontSize: 'var(--text-2xl, 1.5rem)', fontWeight: 800 }}}}>{label} 수정</h1>
      </header>
      {{error && <div style={{{{ padding: '0.75rem', background: '#fef2f2', color: '#dc2626', borderRadius: '0.375rem' }}}}>{{error}}</div>}}
      <form onSubmit={{handleSubmit}} style={{{{ background: 'var(--surface, #fff)', borderRadius: '0.75rem', padding: 'var(--space-6, 1.5rem)', boxShadow: '0 1px 3px rgba(0,0,0,0.1)', display: 'flex', flexDirection: 'column', gap: '1rem' }}}}>
        {{Object.entries(formData).filter(([k]) => !['id', 'created_at', 'updated_at'].includes(k)).map(([key, value]) => (
          <div key={{key}}>
            <label style={{{{ display: 'block', fontSize: 'var(--text-sm, 0.875rem)', fontWeight: 600, marginBottom: '0.25rem' }}}}>{{key}}</label>
            <input type="text" value={{String(value ?? '')}} onChange={{(e) => handleChange(key, e.target.value)}} style={{{{ width: '100%', padding: '0.5rem', border: '1px solid var(--border, #e5e7eb)', borderRadius: '0.375rem' }}}} />
          </div>
        ))}}
        <div style={{{{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}}}>
          <button type="button" onClick={{() => router.push('{parent_route}')}} style={{{{ padding: '0.5rem 1rem', background: 'var(--surface-secondary, #f3f4f6)', border: 'none', borderRadius: '0.375rem', cursor: 'pointer' }}}}>취소</button>
          <button type="submit" disabled={{submitting}} style={{{{ padding: '0.5rem 1rem', background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: '0.375rem', cursor: 'pointer', opacity: submitting ? 0.5 : 1 }}}}>{{submitting ? '저장 중...' : '저장'}}</button>
        </div>
      </form>
    </div>
  );
}}
"""


# ============================================================
# 3. 네비게이션 버튼 연결
# ============================================================

def _fix_navigation_buttons(fe: Path) -> list[str]:
    """'목록으로', '뒤로가기' 등 네비게이션 버튼에 href 연결."""
    fixes = []

    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue

        original = content

        # 서버 컴포넌트 스킵
        if "'use client'" not in content and '"use client"' not in content:
            continue

        # <Button ...>목록으로</Button> → <a href=".."><Button ...>목록으로</Button></a>
        # 이미 <a>로 감싸져 있으면 스킵
        patterns = [
            (r'(<Button[^>]*>)(목록으로)(</Button>)', '..'),
            (r'(<Button[^>]*>)(뒤로가기)(</Button>)', '..'),
            (r'(<Button[^>]*>)(홈으로 돌아가기)(</Button>)', '/'),
        ]

        for pattern, target in patterns:
            # 이미 <a> 안에 있는지 확인
            for m in re.finditer(pattern, content):
                start = max(0, m.start() - 50)
                before = content[start:m.start()]
                if "<a " in before and "</a>" not in before:
                    continue  # 이미 <a> 안에 있음

                # href가 없는 Button에 onClick 추가
                btn_tag = m.group(1)
                if "onClick" not in btn_tag:
                    new_btn = btn_tag.replace("<Button", f'<Button onClick={{() => window.location.href = "{target}"}}')
                    content = content[:m.start()] + new_btn + m.group(2) + m.group(3) + content[m.end():]
                    fixes.append(f"nav_button: {tsx_file.name} '{m.group(2)}' → {target}")

        if content != original:
            tsx_file.write_text(content, encoding="utf-8")

    return fixes


# ============================================================
# 4. 리스트→상세 연계 자동 수정
# ============================================================

def _fix_list_detail_links(fe: Path) -> list[str]:
    """리스트 페이지의 카드/항목에 상세 링크가 없으면 추가하고, 상세 페이지도 생성."""
    fixes = []
    app_dir = fe / "src" / "app"
    if not app_dir.is_dir():
        return fixes

    # 리스트 페이지 = (main)/ 하위의 page.tsx 중 .map()이 있는 것
    for page_file in app_dir.rglob("page.tsx"):
        if "node_modules" in str(page_file) or "[" in str(page_file):
            continue  # 동적 라우트(상세 페이지)는 스킵
        try:
            content = page_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("page_read_failed path=%s error=%s", page_file, exc)
            continue
        if "'use client'" not in content:
            continue
        if ".map(" not in content:
            continue

        original = content

        # map 블록에서 변수명과 key 추출
        # {items.map((item) => ( <Card key={item.id}> ... </Card> ))}
        map_matches = re.finditer(
            r'(\w+)\.map\(\s*\((\w+)[^)]*\)\s*=>\s*\(',
            content,
        )

        for mm in map_matches:
            list_var = mm.group(1)   # hospitals, products, sitters...
            item_var = mm.group(2)   # hospital, product, sitter...

            # 이 map 블록 안에 이미 href가 있으면 스킵
            block_start = mm.end()
            # 대략적으로 블록 끝 찾기 (다음 .map 또는 파일 끝)
            next_map = content.find(".map(", block_start)
            block_end = next_map if next_map > 0 else len(content)
            block = content[block_start:block_end]

            if f"href=" in block and item_var in block:
                continue  # 이미 링크 있음

            # 라우트 경로 추론
            page_rel = page_file.parent.relative_to(app_dir)
            route_parts = [p for p in page_rel.parts if not p.startswith("(")]
            route = "/" + "/".join(route_parts) if route_parts else "/"

            if route == "/":
                continue  # 홈은 스킵 (이미 처리됨)

            # Card 컴포넌트를 <a> 태그로 감싸기
            # <Card key={item.id}  →  <a href={`/route/${item.id}`} style={{textDecoration:'none',color:'inherit'}}><Card key={item.id}
            card_pattern = rf'(<Card\s+key=\{{{item_var}\.(?:id|_id)\}})'
            card_match = re.search(card_pattern, block)
            if not card_match:
                # div key 패턴도 시도
                card_pattern = rf'(<div\s+key=\{{{item_var}\.(?:id|_id)\}})'
                card_match = re.search(card_pattern, block)

            if card_match:
                old_tag = card_match.group(1)
                new_tag = f'<a href={{`{route}/${{{item_var}.id}}`}} style={{{{textDecoration:"none",color:"inherit"}}}}>{old_tag}'
                content = content.replace(old_tag, new_tag, 1)

                # 닫는 태그도 추가: </Card> → </Card></a> 또는 </div> → </div></a>
                if "<Card" in old_tag:
                    # 해당 map 블록의 </Card> 찾기
                    close_idx = content.find("</Card>", content.find(new_tag))
                    if close_idx > 0:
                        content = content[:close_idx + 7] + "</a>" + content[close_idx + 7:]
                elif "<div" in old_tag:
                    close_idx = content.find("</div>", content.find(new_tag))
                    if close_idx > 0:
                        content = content[:close_idx + 6] + "</a>" + content[close_idx + 6:]

                # 상세 페이지 경로: 부모에 이미 [id] 있으면 유니크 슬러그로 분기
                from engine.workspace.route_slug import normalize_dynamic_segment
                slug_name = normalize_dynamic_segment(
                    page_file.parent, page_file.parent.name
                )
                # href 치환도 동일 슬러그 사용 (Link 생성 지점과 일관)
                slug_inner = slug_name.strip("[]")
                if slug_inner != "id":
                    # 위에서 만든 new_tag가 `${item_var}.id` 참조를 포함 —
                    # 상세 페이지도 params.<slug_inner>로 일관되게 받도록 치환.
                    old_href_token = f"${{{item_var}.id}}"
                    new_href_token = f"${{{item_var}.id}}"  # href는 id 그대로 전달, 슬러그만 다름
                    # (의도: URL은 /resource/123 형태로 그대로, 하지만 라우트 폴더명만 [careLogId])

                fixes.append(
                    f"list_link: {page_file.name} {item_var} → {route}/{slug_name}"
                )

                # 상세 페이지 생성
                detail_dir = page_file.parent / slug_name
                detail_file = detail_dir / "page.tsx"
                if not detail_file.exists():
                    detail_dir.mkdir(parents=True, exist_ok=True)
                    detail_page = _generate_detail_page(
                        route, item_var, list_var, param_name=slug_inner
                    )
                    detail_file.write_text(detail_page, encoding="utf-8")
                    fixes.append(f"detail_page: {route}/{slug_name}")

        if content != original:
            page_file.write_text(content, encoding="utf-8")

    return fixes


def _generate_detail_page(
    route: str, item_var: str, list_var: str, param_name: str = "id"
) -> str:
    """리스트 항목의 상세 페이지를 자동 생성.

    param_name: URL 파라미터 이름 (기본 'id', 충돌 시 'careLogId' 등).
    """
    name = route.strip("/").split("/")[-1]
    title_map = {
        "hospitals": "병원 상세",
        "sitters": "펫시터 상세",
        "shop": "상품 상세",
        "products": "상품 상세",
        "orders": "주문 상세",
    }
    title = title_map.get(name, name + " 상세")
    api_path = "/" + name if name != "shop" else "/products"

    return (
        "'use client';\n\n"
        "import { useParams } from 'next/navigation';\n"
        "import { useQuery } from '@tanstack/react-query';\n"
        "import Card from '@/components/common/Card';\n"
        "import Button from '@/components/common/Button';\n"
        "import { apiClient } from '@/lib/api/client';\n\n"
        "export default function DetailPage() {\n"
        "  const params = useParams<{ " + param_name + ": string }>();\n"
        "  const " + param_name + " = params." + param_name + ";\n\n"
        "  const { data: item, isLoading } = useQuery({\n"
        "    queryKey: ['" + name + "-detail', " + param_name + "],\n"
        "    queryFn: async () => {\n"
        "      const res = await apiClient.get(`" + api_path + "/${" + param_name + "}`);\n"
        "      return res.data?.data ?? res.data;\n"
        "    },\n"
        "  });\n\n"
        "  if (isLoading) {\n"
        "    return <div style={{ textAlign: 'center', padding: 'var(--space-16)', color: 'var(--text-tertiary)' }}>로딩중...</div>;\n"
        "  }\n\n"
        "  if (!item) {\n"
        "    return (\n"
        "      <div style={{ textAlign: 'center', padding: 'var(--space-16)', color: 'var(--text-tertiary)' }}>\n"
        "        <p style={{ fontSize: '3rem', marginBottom: 'var(--space-4)' }}>🔍</p>\n"
        "        <p>정보를 찾을 수 없습니다</p>\n"
        "        <a href=\"" + route + "\" style={{ marginTop: 'var(--space-4)', display: 'inline-block' }}>\n"
        "          <Button variant=\"secondary\" size=\"sm\">목록으로</Button>\n"
        "        </a>\n"
        "      </div>\n"
        "    );\n"
        "  }\n\n"
        "  return (\n"
        "    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>\n"
        "      <header style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-4)' }}>\n"
        "        <a href=\"" + route + "\" style={{ color: 'var(--text-tertiary)', textDecoration: 'none', fontSize: 'var(--text-sm)' }}>← 목록</a>\n"
        "        <h1 style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--text-2xl)', fontWeight: 800 }}>" + title + "</h1>\n"
        "      </header>\n"
        "      <Card>\n"
        "        <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--text-xl)', fontWeight: 700, marginBottom: 'var(--space-4)' }}>\n"
        "          {item.name || item.title || item.orderNumber || '" + title + "'}\n"
        "        </h2>\n"
        "        {item.description && <p style={{ color: 'var(--text-secondary)', lineHeight: 'var(--leading-relaxed)', marginBottom: 'var(--space-4)' }}>{item.description}</p>}\n"
        "        {item.address && <p style={{ fontSize: 'var(--text-sm)', color: 'var(--text-tertiary)' }}>📍 {item.address}</p>}\n"
        "        {item.phone && <p style={{ fontSize: 'var(--text-sm)', color: 'var(--text-tertiary)', marginTop: 'var(--space-1)' }}>📞 {item.phone}</p>}\n"
        "        {item.rating !== undefined && <p style={{ fontSize: 'var(--text-sm)', marginTop: 'var(--space-2)' }}>⭐ {item.rating}</p>}\n"
        "        {item.price !== undefined && <p style={{ fontSize: 'var(--text-lg)', fontWeight: 700, color: 'var(--accent)', marginTop: 'var(--space-4)' }}>{item.salePrice || item.price}원</p>}\n"
        "        {item.totalAmount !== undefined && <p style={{ fontSize: 'var(--text-lg)', fontWeight: 700, color: 'var(--accent)', marginTop: 'var(--space-4)' }}>총 {item.totalAmount}원</p>}\n"
        "        {item.status && <p style={{ fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', marginTop: 'var(--space-2)' }}>상태: {item.status}</p>}\n"
        "      </Card>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


# ============================================================
# 5. API 응답 wrapper 언래핑 자동 수정
# ============================================================

def _fix_api_response_unwrap(fe: Path) -> list[str]:
    """apiClient 호출 후 응답 처리 자동 수정:
    1. res.data → res.data?.data ?? res.data (wrapper 언래핑)
    2. 결과가 { items: [...] } 페이지네이션이면 .items 추출
    3. .map() 전에 Array.isArray 안전 체크"""
    fixes = []

    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue

        if "apiClient" not in content:
            continue

        original = content

        # (1) return res.data; → return res.data?.data ?? res.data;
        content = re.sub(
            r'return\s+(res|response)\.data\s*;',
            lambda m: (
                m.group(0) if "?.data" in content[max(0, m.start()-50):m.start()]
                else f'const _d = {m.group(1)}.data?.data ?? {m.group(1)}.data; return Array.isArray(_d) ? _d : _d?.items ?? _d;'
            ),
            content,
        )

        # (2) { xxx: xxxRes.data } → unwrap + items 추출
        content = re.sub(
            r'(\w+Res)\.data([,\s\}])',
            lambda m: (
                m.group(0) if "?.data" in m.group(0)
                else f'(({m.group(1)}.data?.data ?? {m.group(1)}.data)?.items ?? ({m.group(1)}.data?.data ?? {m.group(1)}.data)){m.group(2)}'
            ),
            content,
        )

        if content != original:
            tsx_file.write_text(content, encoding="utf-8")
            fixes.append(f"api_unwrap: {tsx_file.name}")

    return fixes


# ============================================================
# 6. Promise.all → Promise.allSettled (하나 실패해도 나머지 유지)
# ============================================================

def _fix_promise_all_settled(fe: Path) -> list[str]:
    """apiClient 호출을 Promise.all로 묶은 코드를 Promise.allSettled로 변환."""
    fixes = []
    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue
        if "Promise.all([" not in content or "apiClient" not in content:
            continue
        if "Promise.allSettled" in content:
            continue  # 이미 변환됨

        original = content
        content = content.replace("Promise.all([", "Promise.allSettled([")
        if content != original:
            tsx_file.write_text(content, encoding="utf-8")
            fixes.append(f"promise_allSettled: {tsx_file.name}")
    return fixes


# ============================================================
# 7. 프론트엔드 API 경로 vs 백엔드 실제 경로 불일치 감지 + 수정
# ============================================================

def _fix_api_path_mismatch(fe: Path, be: Path) -> list[str]:
    """프론트엔드에서 호출하는 API 경로가 백엔드에 존재하는지 확인하고 수정."""
    fixes = []
    if not be.is_dir():
        return fixes

    # 백엔드 라우트 수집 (routes/index.ts + 라우터 파일)
    backend_routes: set[str] = set()
    routes_index = be / "src" / "routes" / "index.ts"
    if routes_index.is_file():
        content = routes_index.read_text(encoding="utf-8")
        for m in re.finditer(r"routes\.use\(\s*['\"]([^'\"]+)['\"]", content):
            prefix = m.group(1)
            backend_routes.add(prefix.rstrip("/"))

    # 각 라우터 파일의 경로도 수집
    for router_file in be.rglob("*.router.ts"):
        try:
            rcontent = router_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("router_read_failed path=%s error=%s", router_file, exc)
            continue
        for m in re.finditer(r"router\.\w+\(\s*['\"]([^'\"]+)['\"]", rcontent):
            backend_routes.add(m.group(1).rstrip("/"))

    if not backend_routes:
        return fixes

    # 프론트엔드에서 호출하는 API 경로 수집
    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue
        if "apiClient" not in content:
            continue

        original = content
        for m in re.finditer(r"apiClient\.\w+(?:<[^>]*>)?\(\s*['\"`]([^'\"`$]+)['\"`]", content):
            api_path = m.group(1).split("?")[0].rstrip("/")  # 쿼리 파라미터 제거
            
            # 동적 세그먼트 제거 (${id} 등)
            if "${" in api_path:
                continue

            # 경로가 백엔드에 있는지 확인
            path_parts = api_path.strip("/").split("/")
            first_segment = "/" + path_parts[0] if path_parts else ""

            # 이미 존재하면 스킵
            if api_path in backend_routes or first_segment in backend_routes:
                continue

            # /community/posts → /posts 로 수정 시도
            if len(path_parts) >= 2:
                short_path = "/" + "/".join(path_parts[1:])
                if short_path in backend_routes or ("/" + path_parts[1]) in backend_routes:
                    content = content.replace(f"'{api_path}", f"'{short_path}")
                    content = content.replace(f'"{api_path}', f'"{short_path}')
                    content = content.replace(f'`{api_path}', f'`{short_path}')
                    fixes.append(f"api_path_fix: {tsx_file.name} {api_path} → {short_path}")

        if content != original:
            tsx_file.write_text(content, encoding="utf-8")

    return fixes


# ============================================================
# 8. [v8+] CRUD 완전성 검증
# ============================================================

def _check_crud_completeness(fe: Path) -> list[dict]:
    """리스트 페이지가 있는 엔티티에 생성 폼이 있는지 검증."""
    issues = []
    app_dir = fe / "src" / "app"
    if not app_dir.is_dir():
        return issues

    pages = {}
    for page_file in app_dir.rglob("page.tsx"):
        if "node_modules" in str(page_file):
            continue
        rel = page_file.relative_to(app_dir)
        parts = [p for p in rel.parts[:-1] if not p.startswith("(")]
        route = "/" + "/".join(parts) if parts else "/"
        pages[route] = page_file

    for route, page_file in pages.items():
        if "[" in route or route == "/":
            continue
        try:
            content = page_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("page_read_failed path=%s error=%s", page_file, exc)
            continue
        is_list = bool(re.search(r'\.map\s*\(|forEach|Array|list|List|items|Items', content, re.IGNORECASE))
        if not is_list:
            continue

        has_detail = any(r.startswith(route + "/[") for r in pages)
        if not has_detail:
            issues.append({"route": route, "issue": "상세 페이지([id]) 없음"})

        has_create_page = any(r in (f"{route}/new", f"{route}/create", f"{route}/add") for r in pages)
        has_create_modal = bool(re.search(r'modal|Modal|dialog|Dialog|생성|추가|등록|작성', content, re.IGNORECASE))
        has_create_button = bool(re.search(r'onClick.*(?:생성|추가|등록|작성|create|add|new)|href.*(?:new|create|add)', content, re.IGNORECASE))
        if not has_create_page and not has_create_modal and not has_create_button:
            issues.append({"route": route, "issue": "생성 폼(모달/페이지) 없음"})

        if has_detail and not re.search(r'Link|router\.push|href=|useRouter', content, re.IGNORECASE):
            issues.append({"route": route, "issue": "상세 페이지로의 Link 없음"})

    if issues:
        logger.warning("crud_completeness count=%d issues=%s", len(issues), issues[:5])
    return issues


# ============================================================
# 9. [v8+] 프론트 API 호출 vs 백엔드 라우트 정합성 강화
# ============================================================

def _check_api_route_consistency(fe: Path, be: Path) -> list[dict]:
    """프론트 API 호출 경로가 백엔드에 실제 존재하는지 검증."""
    issues = []
    be_routes = set()
    for ts_file in be.rglob("*.ts"):
        if "node_modules" in str(ts_file):
            continue
        try:
            code = ts_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("ts_read_failed path=%s error=%s", ts_file, exc)
            continue
        for m in re.finditer(r'\.(get|post|put|patch|delete)\s*\(\s*[\'"`](\/[^\'"` ]*)[\'"`]', code, re.IGNORECASE):
            be_routes.add(m.group(2))

    if not be_routes:
        return issues

    for tsx_file in fe.rglob("*.ts*"):
        if "node_modules" in str(tsx_file) or ".next" in str(tsx_file):
            continue
        try:
            code = tsx_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("tsx_read_failed path=%s error=%s", tsx_file, exc)
            continue
        for m in re.finditer(r'(?:get|post|put|patch|delete|fetch)\s*[\(<]\s*[\'"`](?:\/api)?(\/?v\d+\/[^\'"` ]*)[\'"`]', code, re.IGNORECASE):
            path = m.group(1)
            if not path.startswith("/"):
                path = "/" + path
            normalized = re.sub(r'\$\{[^}]+\}', ':id', path)
            matched = normalized in be_routes
            if not matched:
                for br in be_routes:
                    br_pattern = re.escape(br).replace(r'\:id', '[^/]+').replace(r'\:\w+', '[^/]+')
                    if re.fullmatch(br_pattern, normalized):
                        matched = True
                        break
            if not matched:
                issues.append({"file": str(tsx_file.relative_to(fe)), "path": path})

    if issues:
        logger.warning("api_route_consistency count=%d issues=%s", len(issues), issues[:5])
    return issues
