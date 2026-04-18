from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from engine.core.dag_advancer import NodeSnapshot
from engine.skills.utils import _now, _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 필수 UX 컴포넌트 자동 등록용 템플릿
# ---------------------------------------------------------------------------

_REQUIRED_UX_TEMPLATES: dict[str, dict] = {
    "error_boundary": {
        "category": "ux",
        "description": "에러 바운더리 — 예외 발생 시 폴백 UI와 재시도 버튼을 표시합니다.",
        "html_template": (
            '<div class="error-boundary" role="alert" aria-live="assertive">\n'
            '  <div class="error-boundary__icon">\n'
            '    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n'
            '      <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>\n'
            '      <line x1="12" y1="16" x2="12.01" y2="16"/>\n'
            '    </svg>\n'
            '  </div>\n'
            '  <h2 class="error-boundary__title">{{title}}</h2>\n'
            '  <p class="error-boundary__message">{{message}}</p>\n'
            '  <button class="error-boundary__retry" onclick="window.location.reload()">\n'
            '    다시 시도\n'
            '  </button>\n'
            '</div>'
        ),
        "css": (
            '.error-boundary { display:flex; flex-direction:column; align-items:center; justify-content:center;\n'
            '  padding:var(--space-xl,32px); text-align:center; min-height:200px; }\n'
            '.error-boundary__icon { color:var(--color-status-error,#ef4444); margin-bottom:var(--space-md,16px); }\n'
            '.error-boundary__title { font-size:var(--font-lg,1.25rem); font-weight:var(--font-weight-bold,700);\n'
            '  color:var(--color-text-primary,#1a1a2e); margin-bottom:var(--space-sm,8px); }\n'
            '.error-boundary__message { color:var(--color-text-secondary,#64748b);\n'
            '  margin-bottom:var(--space-lg,24px); max-width:480px; }\n'
            '.error-boundary__retry { padding:var(--space-sm,8px) var(--space-lg,24px);\n'
            '  background:var(--color-accent-primary,#2563EB); color:#fff; border:none;\n'
            '  border-radius:var(--radius-md,8px); cursor:pointer; font-size:var(--font-sm,0.875rem); }\n'
            '.error-boundary__retry:hover { opacity:0.9; }'
        ),
        "slots": {
            "title": {"type": "text", "required": False, "default": "문제가 발생했습니다"},
            "message": {"type": "text", "required": False, "default": "잠시 후 다시 시도해주세요."},
        },
    },
    "loading_indicator": {
        "category": "ux",
        "description": "로딩 인디케이터 — 페이지/섹션 로딩 시 스피너와 스켈레톤을 표시합니다.",
        "html_template": (
            '<div class="loading-indicator" role="status" aria-label="로딩 중">\n'
            '  <div class="loading-indicator__spinner"></div>\n'
            '  <p class="loading-indicator__text">{{message}}</p>\n'
            '</div>'
        ),
        "css": (
            '.loading-indicator { display:flex; flex-direction:column; align-items:center;\n'
            '  justify-content:center; padding:var(--space-xl,32px); min-height:120px; }\n'
            '.loading-indicator__spinner { width:40px; height:40px;\n'
            '  border:3px solid var(--color-border-subtle,#e2e8f0);\n'
            '  border-top-color:var(--color-accent-primary,#2563EB); border-radius:50%;\n'
            '  animation:li-spin 0.8s linear infinite; }\n'
            '@keyframes li-spin { to { transform:rotate(360deg); } }\n'
            '.loading-indicator__text { margin-top:var(--space-md,16px);\n'
            '  color:var(--color-text-secondary,#64748b); font-size:var(--font-sm,0.875rem); }'
        ),
        "slots": {
            "message": {"type": "text", "required": False, "default": "불러오는 중..."},
        },
    },
    "modal_container": {
        "category": "ux",
        "description": "모달 컨테이너 — 오버레이 배경과 닫기 버튼이 포함된 다이얼로그 컨테이너입니다.",
        "html_template": (
            '<div class="modal-overlay" id="modal-root" style="display:none" role="dialog" aria-modal="true">\n'
            '  <div class="modal-backdrop" onclick="this.parentElement.style.display=\'none\'"></div>\n'
            '  <div class="modal-container">\n'
            '    <button class="modal-close" onclick="this.closest(\'.modal-overlay\').style.display=\'none\'"'
            ' aria-label="닫기">&times;</button>\n'
            '    <div class="modal-header"><h3>{{title}}</h3></div>\n'
            '    <div class="modal-body">{{content}}</div>\n'
            '    <div class="modal-footer">{{footer}}</div>\n'
            '  </div>\n'
            '</div>'
        ),
        "css": (
            '.modal-overlay { position:fixed; inset:0; z-index:1000; display:flex;\n'
            '  align-items:center; justify-content:center; }\n'
            '.modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,0.5); }\n'
            '.modal-container { position:relative; background:var(--color-bg-primary,#fff);\n'
            '  border-radius:var(--radius-lg,14px); padding:var(--space-lg,24px);\n'
            '  max-width:560px; width:90%; max-height:85vh; overflow-y:auto;\n'
            '  box-shadow:var(--shadow-lg,0 10px 25px rgba(0,0,0,0.15)); }\n'
            '.modal-close { position:absolute; top:var(--space-sm,8px); right:var(--space-sm,8px);\n'
            '  background:none; border:none; font-size:1.5rem; cursor:pointer;\n'
            '  color:var(--color-text-secondary,#64748b); line-height:1; }\n'
            '.modal-close:hover { color:var(--color-text-primary,#1a1a2e); }\n'
            '.modal-header h3 { margin:0 0 var(--space-md,16px); font-size:var(--font-lg,1.25rem); }\n'
            '.modal-body { margin-bottom:var(--space-md,16px); }\n'
            '.modal-footer { display:flex; justify-content:flex-end; gap:var(--space-sm,8px); }'
        ),
        "slots": {
            "title": {"type": "text", "required": False, "default": ""},
            "content": {"type": "html", "required": False, "default": ""},
            "footer": {"type": "html", "required": False, "default": ""},
        },
    },
    "toast_container": {
        "category": "ux",
        "description": "토스트 알림 컨테이너 — 자동 소멸 알림 메시지를 표시합니다.",
        "html_template": (
            '<div class="toast-container" id="toast-root" aria-live="polite"></div>\n'
            '<template id="toast-tpl">\n'
            '  <div class="toast toast--{{type}}" role="alert">\n'
            '    <span class="toast__message">{{message}}</span>\n'
            '    <button class="toast__close" onclick="this.parentElement.remove()">&times;</button>\n'
            '  </div>\n'
            '</template>'
        ),
        "css": (
            '.toast-container { position:fixed; bottom:var(--space-lg,24px); right:var(--space-lg,24px);\n'
            '  z-index:2000; display:flex; flex-direction:column-reverse; gap:var(--space-sm,8px);\n'
            '  pointer-events:none; }\n'
            '.toast { display:flex; align-items:center; gap:var(--space-sm,8px);\n'
            '  padding:var(--space-sm,8px) var(--space-md,16px);\n'
            '  border-radius:var(--radius-md,8px); pointer-events:auto;\n'
            '  background:var(--color-bg-primary,#fff); color:var(--color-text-primary,#1a1a2e);\n'
            '  box-shadow:var(--shadow-md,0 4px 12px rgba(0,0,0,0.1));\n'
            '  animation:toast-in 0.3s ease-out; }\n'
            '.toast--success { border-left:4px solid var(--color-status-success,#22c55e); }\n'
            '.toast--error { border-left:4px solid var(--color-status-error,#ef4444); }\n'
            '.toast--warning { border-left:4px solid var(--color-status-warning,#f59e0b); }\n'
            '.toast--info { border-left:4px solid var(--color-status-info,#3b82f6); }\n'
            '.toast__close { background:none; border:none; cursor:pointer;\n'
            '  color:var(--color-text-secondary,#64748b); font-size:1.25rem; line-height:1; }\n'
            '@keyframes toast-in { from { opacity:0; transform:translateY(16px); }\n'
            '  to { opacity:1; transform:translateY(0); } }'
        ),
        "slots": {
            "type": {"type": "text", "required": False, "default": "info"},
            "message": {"type": "text", "required": False, "default": ""},
        },
    },
    "empty_state": {
        "category": "ux",
        "description": "빈 상태 표시 — 데이터가 없을 때 안내 메시지와 액션 버튼을 표시합니다.",
        "html_template": (
            '<div class="empty-state">\n'
            '  <div class="empty-state__icon">\n'
            '    <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">\n'
            '      <path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>\n'
            '    </svg>\n'
            '  </div>\n'
            '  <h3 class="empty-state__title">{{title}}</h3>\n'
            '  <p class="empty-state__description">{{description}}</p>\n'
            '  <div class="empty-state__action">{{action}}</div>\n'
            '</div>'
        ),
        "css": (
            '.empty-state { display:flex; flex-direction:column; align-items:center;\n'
            '  justify-content:center; padding:var(--space-2xl,48px) var(--space-lg,24px);\n'
            '  text-align:center; }\n'
            '.empty-state__icon { color:var(--color-text-secondary,#64748b); opacity:0.5;\n'
            '  margin-bottom:var(--space-md,16px); }\n'
            '.empty-state__title { font-size:var(--font-lg,1.25rem);\n'
            '  font-weight:var(--font-weight-bold,700); color:var(--color-text-primary,#1a1a2e);\n'
            '  margin:0 0 var(--space-xs,4px); }\n'
            '.empty-state__description { color:var(--color-text-secondary,#64748b);\n'
            '  margin-bottom:var(--space-lg,24px); max-width:400px; }\n'
            '.empty-state__action:empty { display:none; }'
        ),
        "slots": {
            "title": {"type": "text", "required": False, "default": "데이터가 없습니다"},
            "description": {"type": "text", "required": False, "default": "아직 표시할 내용이 없습니다."},
            "action": {"type": "html", "required": False, "default": ""},
        },
    },
}


def _build_generic_component_template(name: str) -> dict:
    """알 수 없는 컴포넌트에 대한 범용 스텁 템플릿 생성."""
    display_name = name.replace("_", " ").replace("-", " ").title()
    return {
        "category": "auto_generated",
        "description": f"{display_name} 컴포넌트 (자동 생성)",
        "html_template": (
            f'<section class="{name}" data-component="{name}">\n'
            f'  <div class="{name}__header">\n'
            f'    <h3 class="{name}__title">{{{{title}}}}</h3>\n'
            f'  </div>\n'
            f'  <div class="{name}__body">{{{{content}}}}</div>\n'
            f'</section>'
        ),
        "css": (
            f'.{name} {{ padding:var(--space-lg,24px); background:var(--color-bg-surface,#f8fafc);\n'
            f'  border-radius:var(--radius-md,8px); border:1px solid var(--color-border-subtle,#e2e8f0); }}\n'
            f'.{name}__title {{ font-size:var(--font-lg,1.25rem); font-weight:var(--font-weight-bold,700);\n'
            f'  color:var(--color-text-primary,#1a1a2e); margin:0 0 var(--space-md,16px); }}\n'
            f'.{name}__body {{ color:var(--color-text-primary,#1a1a2e); }}'
        ),
        "slots": {
            "title": {"type": "text", "required": False, "default": display_name},
            "content": {"type": "html", "required": False, "default": ""},
        },
    }


async def _auto_register_missing_components(
    db: Any, project_id: str, missing_names: set[str]
) -> int:
    """누락된 컴포넌트를 자동으로 composition_components에 등록.

    REQUIRED_UX 컴포넌트는 완성도 높은 구현을 사용하고,
    그 외 미지 컴포넌트는 범용 스텁을 생성한다.
    AI 호출 0회, 토큰 비용 0.

    Returns: 등록된 컴포넌트 수.
    """
    from engine.composition.registry import CompositionRegistry, Component

    registry = CompositionRegistry(db)

    # Bulk upsert: missing_names의 모든 컴포넌트를 한번에 등록 (N+1 최적화)
    components = []
    for name in sorted(missing_names):
        tpl = _REQUIRED_UX_TEMPLATES.get(name) or _build_generic_component_template(name)
        comp = Component(
            name=name,
            category=tpl["category"],
            description=tpl["description"],
            html_template=tpl["html_template"],
            css=tpl["css"],
            slots=tpl["slots"],
        )
        components.append((name, tpl, comp))

    if components:
        await registry.save_components_bulk_upsert(project_id, [c[2] for c in components])
        for name, tpl, _ in components:
            logger.info(
                "auto_registered_component project=%s name=%s category=%s",
                project_id, name, tpl["category"],
            )

    return len(components)


async def _handle_assembly_node(db: Any, node: NodeSnapshot, spec: dict) -> None:
    """assembly 노드: AI 호출 없이 CompositionRenderer로 전체 페이지 조립.

    토큰 + 컴포넌트 + 레시피가 composition registry에 저장된 상태에서
    렌더러가 이를 읽어 HTML을 생성한다.  AI 호출 0회, 토큰 비용 0.

    코어 불변: DAGAdvancer, StateMachine, ContextAssembler 미변경.
    """
    from engine.composition.registry import CompositionRegistry
    from engine.composition.renderer import CompositionRenderer
    from engine.skills.artifact.saver import _save_artifact

    now = _now()
    comp_registry = CompositionRegistry(db)
    renderer = CompositionRenderer(comp_registry)

    results = await renderer.render_all_pages(node.project_id)

    if not results:
        raise ValueError(
            "조립할 페이지 레시피가 없습니다. "
            "디자인 토큰/컴포넌트 라이브러리/페이지 레시피 노드가 먼저 완료되어야 합니다."
        )

    # 누락 컴포넌트 집계
    all_missing = set()
    for result in results:
        for w in (result.warnings or []):
            if "컴포넌트 없음:" in w:
                comp_name = w.split("컴포넌트 없음:")[-1].strip()
                all_missing.add(comp_name)

    if all_missing:
        logger.warning(
            "composition_missing_components project=%s missing=%d names=%s",
            node.project_id, len(all_missing), list(all_missing)[:10],
        )

        # ── 1단계: 누락 컴포넌트 자동 등록 (AI 호출 0회, 즉시 해결) ──
        auto_count = await _auto_register_missing_components(
            db, node.project_id, all_missing,
        )

        if auto_count > 0:
            logger.info(
                "composition_auto_registered project=%s count=%d — re-rendering",
                node.project_id, auto_count,
            )
            # 새로 등록된 컴포넌트로 즉시 재렌더링
            results = await renderer.render_all_pages(node.project_id)

            # 재렌더링 후에도 누락이 남아있는지 확인
            still_missing = set()
            for result in results:
                for w in (result.warnings or []):
                    if "컴포넌트 없음:" in w:
                        comp_name = w.split("컴포넌트 없음:")[-1].strip()
                        still_missing.add(comp_name)

            if still_missing:
                logger.warning(
                    "composition_still_missing_after_auto project=%s missing=%s",
                    node.project_id, list(still_missing)[:10],
                )
                # 여전히 누락 → 남은 것들도 자동 등록 2차 시도
                auto_count2 = await _auto_register_missing_components(
                    db, node.project_id, still_missing,
                )
                if auto_count2 > 0:
                    results = await renderer.render_all_pages(node.project_id)
        else:
            # 자동 등록이 0건 (이미 등록됨 but 렌더러가 못 찾는 경우) → 경고 후 진행
            logger.warning(
                "composition_auto_register_zero project=%s missing=%s — proceeding with warnings",
                node.project_id, list(all_missing)[:10],
            )

    # 각 페이지를 개별 artifact로 저장
    for result in results:
        await _save_artifact(db, node, result.html, "html")
        logger.info(
            "composition_assembled page=%s hash=%s warnings=%s",
            result.page_slug, result.content_hash,
            result.warnings[:3] if result.warnings else "none",
        )

    # 전체 결과 요약을 메인 artifact로 저장
    summary = _build_assembly_summary(results)
    await _save_artifact(db, node, summary, "document")

    # 페이지 그룹 분할: 페이지 수 > SPLIT_THRESHOLD이면 독립 노드 생성
    SPLIT_THRESHOLD = 4
    if len(results) > SPLIT_THRESHOLD:
        from engine.skills.splitting import _split_frontend_component_nodes
        await _split_frontend_component_nodes(db, node, results)

    # 노드 완료
    await db.execute(
        "UPDATE nodes SET state='COMPLETED', completed_at=?, updated_at=? WHERE id=?",
        (now, now, node.id),
    )
    logger.info(
        "composition_assembly_complete project=%s pages=%d",
        node.project_id, len(results),
    )


async def _composition_post_process(
    db: Any, node: NodeSnapshot, content: str, role: str
) -> None:
    """AI가 생성한 JSON 산출물을 composition registry에 저장하는 후처리.

    executor 파이프라인에서 artifact 저장(step 9) 직후에 호출.
    기존 파이프라인을 변경하지 않고, 추가 저장만 수행.

    역할:
      tokens  — DesignTokens로 파싱 → registry 저장
      library — Component 배열로 파싱 → registry 저장
      recipe  — PageRecipe 배열로 파싱 → registry 저장
    """
    from engine.composition.registry import (
        CompositionRegistry,
        _dict_to_tokens,
        _dict_to_component,
        _dict_to_recipe,
    )

    comp_registry = CompositionRegistry(db)

    # JSON 추출 (AI가 코드블록으로 감쌀 수 있으므로 정리)
    clean_json = _extract_json(content)
    parsed = None

    if clean_json:
        try:
            parsed = json.loads(clean_json)
        except (json.JSONDecodeError, ValueError):
            # 후행 쉼표/주석 제거 후 재시도
            try:
                import re as _re_comp
                cleaned = _re_comp.sub(r',\s*([}\]])', r'\1', clean_json)
                parsed = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                pass

    if parsed is None:
        logger.warning(
            "composition_json_extract_failed node=%s role=%s — content preview: %s",
            node.id, role, content[:200],
        )
        return

    if role == "tokens":
        tokens_dict = parsed if isinstance(parsed, dict) else {}
        tokens_dict["project_id"] = node.project_id
        tokens = _dict_to_tokens(tokens_dict)
        tokens.project_id = node.project_id
        await comp_registry.save_tokens(tokens)
        logger.info("composition_tokens_registered project=%s", node.project_id)

    elif role == "library":
        components = parsed if isinstance(parsed, list) else [parsed]
        count = 0
        for comp_dict in components:
            comp = _dict_to_component(comp_dict)
            await comp_registry.save_component(node.project_id, comp)
            count += 1
        logger.info(
            "composition_library_registered project=%s components=%d",
            node.project_id, count,
        )

    elif role == "recipe":
        recipes = parsed if isinstance(parsed, list) else [parsed]

        # 구조 감지: 페이지 단위 vs 컴포넌트 단위
        has_placements = any(isinstance(r, dict) and "placements" in r for r in recipes)
        has_component_only = any(isinstance(r, dict) and "component_name" in r and "placements" not in r for r in recipes)

        if has_component_only and not has_placements:
            # AI가 컴포넌트 단위로 반환 → 레지스트리에서 페이지 구조를 가져와 자동 재구성
            logger.warning(
                "composition_recipe_auto_restructure node=%s — 컴포넌트 단위 %d건 → 레지스트리 기반 페이지 재구성",
                node.id, len(recipes),
            )
            recipes = await _restructure_placements_to_pages(
                db, node.project_id, recipes
            )

        import re as _rslug
        valid_recipes = []
        for recipe_dict in recipes:
            if not isinstance(recipe_dict, dict):
                continue
            if "placements" not in recipe_dict:
                continue
            if not recipe_dict.get("page_slug") and recipe_dict.get("page_name"):
                slug = _rslug.sub(r'[^a-z0-9]+', '-', recipe_dict["page_name"].lower()).strip('-')
                recipe_dict["page_slug"] = slug or f"page-{len(valid_recipes)}"
            if not recipe_dict.get("page_slug"):
                recipe_dict["page_slug"] = f"page-{len(valid_recipes)}"
            valid_recipes.append(recipe_dict)

        count = 0
        for recipe_dict in valid_recipes:
            recipe_dict["project_id"] = node.project_id
            recipe = _dict_to_recipe(recipe_dict)
            recipe.project_id = node.project_id
            await comp_registry.save_recipe(recipe)
            count += 1
        logger.info(
            "composition_recipes_registered project=%s pages=%d",
            node.project_id, count,
        )


async def _restructure_placements_to_pages(
    db: Any, project_id: str, component_placements: list
) -> list:
    """컴포넌트 단위 배열을 레지스트리의 페이지 구조를 참조해 페이지 단위로 재구성.

    AI가 [{component_name, order, bindings}, ...] 형태로 반환했을 때,
    컴포넌트 레지스트리의 pages 구조를 가져와서
    [{page_name, page_slug, placements: [...]}, ...] 형태로 변환.
    """
    # 레지스트리 산출물에서 pages 구조 로드
    registry_node = await db.fetchone(
        "SELECT n.id FROM nodes n WHERE n.name='컴포넌트 레지스트리' AND n.project_id=? AND n.node_type='TASK'",
        (project_id,),
    )
    if not registry_node:
        logger.warning("restructure_no_registry project=%s", project_id)
        return []

    registry_row = await db.fetchone(
        """SELECT av.storage_path FROM artifact_versions av
           JOIN artifacts a ON av.artifact_id = a.id
           WHERE a.node_id = ? AND av.version_num = a.current_version""",
        (registry_node["id"],),
    )
    if not registry_row:
        return []

    try:
        registry_data = json.loads(registry_row["storage_path"])
    except (json.JSONDecodeError, ValueError):
        return []

    # 레지스트리에서 페이지 목록 추출
    pages = registry_data.get("pages", []) if isinstance(registry_data, dict) else []
    if not pages:
        return []

    # 컴포넌트 placements를 이름으로 인덱싱
    placement_by_name = {}
    for p in component_placements:
        if isinstance(p, dict) and "component_name" in p:
            name = p["component_name"]
            if name not in placement_by_name:
                placement_by_name[name] = []
            placement_by_name[name].append(p)

    # 각 레지스트리 페이지에 대해 레시피 생성
    result = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_name = page.get("page_name", "")
        page_slug = page.get("page_slug", "")
        layout = page.get("layout", "single-column")
        area = page.get("area", "FO")

        placements = []
        page_components = page.get("components", [])
        for comp in page_components:
            if not isinstance(comp, dict):
                continue
            comp_name = comp.get("component_name", "")
            # AI가 생성한 바인딩이 있으면 사용, 없으면 레지스트리의 slots_mapping에서 생성
            if comp_name in placement_by_name and placement_by_name[comp_name]:
                placement = placement_by_name[comp_name].pop(0).copy()
            else:
                # 레지스트리의 slots_mapping을 기본 바인딩으로 변환
                bindings = []
                for slot_name, slot_val in comp.get("slots_mapping", {}).items():
                    bindings.append({"slot_name": slot_name, "value": str(slot_val)})
                placement = {
                    "component_name": comp_name,
                    "order": comp.get("order", 0),
                    "bindings": bindings,
                    "condition": comp.get("condition", ""),
                    "repeat": comp.get("repeat", ""),
                    "wrapper_css_class": "",
                }
            placements.append(placement)

        recipe = {
            "page_name": page_name,
            "page_slug": page_slug,
            "title": page_name,
            "description": "",
            "layout": layout,
            "placements": placements,
            "page_css": "",
            "page_js": "",
        }
        result.append(recipe)

    logger.info(
        "restructure_complete project=%s pages=%d from_placements=%d",
        project_id, len(result), len(component_placements),
    )
    return result


async def _repair_json_if_needed(
    model_adapter, model: str, response, max_tokens: int,
    max_repair_attempts: int = 2,
):
    """JSON 산출물 검증 — 파싱 실패 시 AI에게 수정 요청.

    1차: _extract_json + 기본 정리로 시도
    2차: AI에게 에러 위치와 함께 수정 요청 (repair 호출)
    """
    import json as _jr

    content = response.content

    # 응답 잘림 감지: max_tokens 도달 시 열린 괄호 자동 닫기 (AI repair 전 프로그래매틱 복구)
    if response.stop_reason == "max_tokens":
        import re as _rt
        logger.warning(
            "json_repair_truncated size=%d — attempting bracket closure",
            len(content),
        )
        open_braces = content.count('{') - content.count('}')
        open_brackets = content.count('[') - content.count(']')
        if open_braces > 0 or open_brackets > 0:
            content = _rt.sub(r',\s*$', '', content.rstrip())
            content += ']' * max(open_brackets, 0) + '}' * max(open_braces, 0)

    # JSON 추출 시도
    extracted = _extract_json(content)
    if extracted:
        try:
            _jr.loads(extracted)
            # 유효한 JSON — 응답 교체
            response = type(response)(
                content=extracted,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                model=response.model,
                stop_reason=response.stop_reason,
            )
            return response
        except (_jr.JSONDecodeError, ValueError):
            content = extracted

    # 기본 정리: SELF_CHECK 제거, 이중따옴표, JS 표현식
    import re as _rr
    if "<!-- SELF_CHECK" in content:
        content = content[:content.index("<!-- SELF_CHECK")].rstrip()
    content = _rr.sub(r'""([^"]+)""', r'"\1"', content)
    content = _rr.sub(r'"\s*\+\s*"([^"]*?)"\s*\+\s*"', lambda m: f' {{{m.group(1)}}} ', content)
    content = _rr.sub(r'"\s*\+\s*"', ' ', content)

    # [ 또는 { 시작점 정리
    for i, ch in enumerate(content):
        if ch in ('[', '{'):
            content = content[i:]
            break
    for i in range(len(content) - 1, -1, -1):
        if content[i] in (']', '}'):
            content = content[:i + 1]
            break

    try:
        _jr.loads(content)
        response = type(response)(
            content=content,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
        )
        return response
    except (_jr.JSONDecodeError, ValueError) as first_err:
        pass

    # AI repair 호출 — 에러 위치 집중 컨텍스트 (전체 JSON 대신 ±500자 + 구조 스켈레톤)
    def _build_json_repair_context(json_str: str, err_pos: int, window: int = 500) -> str:
        """에러 위치 주변 윈도우 + JSON 키 구조 스켈레톤 (최대 3K)."""
        # 에러 위치 주변 윈도우
        start = max(0, err_pos - window)
        end = min(len(json_str), err_pos + window)
        error_window = json_str[start:end]
        marker_pos = err_pos - start
        marker_line = " " * min(marker_pos, 80) + "^^^^ ERROR HERE"

        # JSON 키 구조 스켈레톤 (깊이 2까지, 값은 "..." 축약)
        import re as _rsk
        keys = _rsk.findall(r'"([^"]{1,60})"\s*:', json_str[:5000])
        skeleton = ", ".join(f'"{k}": ...' for k in dict.fromkeys(keys))
        if len(skeleton) > 500:
            skeleton = skeleton[:500] + " ..."
        skeleton_block = f"{{ {skeleton} }}" if skeleton else "(구조 추출 불가)"

        ctx = (
            f"## 에러 위치 (position {err_pos}, 전체 {len(json_str)}자 중)\n"
            f"```\n{error_window}\n{marker_line}\n```\n\n"
            f"## JSON 키 구조\n```\n{skeleton_block}\n```"
        )
        return ctx[:3000]

    for attempt in range(max_repair_attempts):
        try:
            err = first_err
            repair_context = _build_json_repair_context(content, err.pos if hasattr(err, 'pos') and err.pos else 0)
            repair_prompt = (
                "아래 JSON에 구문 오류가 있습니다. 수정해서 유효한 JSON만 출력하세요.\n"
                "설명 텍스트 없이 순수 JSON만 출력하세요.\n"
                "문자열 값 안에 따옴표 중첩(\"\"), JS 표현식(\"값\" + \"변수\"), "
                "이스케이프(\\\"를) 절대 사용하지 마세요.\n\n"
                f"오류: {err.msg} at position {err.pos}\n\n"
                f"{repair_context}"
            )

            repair_resp = await model_adapter.call(
                model=model,
                system="유효한 JSON만 출력하세요. 설명/코멘트 없이 순수 JSON만.",
                prompt=repair_prompt,
                max_tokens=max_tokens,
            )

            repaired = _extract_json(repair_resp.content)
            if repaired:
                _jr.loads(repaired)
                logger.info(
                    "json_repair_success attempt=%d original_size=%d repaired_size=%d",
                    attempt + 1, len(content), len(repaired),
                )
                return type(response)(
                    content=repaired,
                    input_tokens=response.input_tokens + repair_resp.input_tokens,
                    output_tokens=response.output_tokens + repair_resp.output_tokens,
                    model=response.model,
                    stop_reason=repair_resp.stop_reason,
                )
        except (_jr.JSONDecodeError, ValueError) as exc:
            first_err = exc
            logger.warning("json_repair_attempt_failed attempt=%d error=%s", attempt + 1, str(exc))
            continue
        except Exception as exc:
            logger.warning("json_repair_call_failed attempt=%d error=%s", attempt + 1, str(exc))
            break

    # 모든 repair 실패 — 원본 반환 (best effort)
    logger.warning("json_repair_exhausted returning_original size=%d", len(content))
    return type(response)(
        content=content,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        model=response.model,
        stop_reason=response.stop_reason,
    )


def _build_assembly_summary(results) -> str:
    """조립 결과 요약 마크다운."""

    lines = ["# 페이지 조립 결과\n"]
    lines.append(f"총 {len(results)}개 페이지 조립 완료\n")
    lines.append("| 페이지 | 슬러그 | 컴포넌트 수 | 해시 | 경고 |")
    lines.append("|--------|--------|------------|------|------|")

    for r in results:
        warnings = ", ".join(r.warnings[:2]) if r.warnings else "-"
        lines.append(
            f"| {r.page_name} | {r.page_slug} | "
            f"{len(r.components_used)} | {r.content_hash} | {warnings} |"
        )

    return "\n".join(lines)
