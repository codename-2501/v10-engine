from __future__ import annotations

import json
import logging
import re
from typing import Any

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


def _group_pages_for_batching(
    recipes: list[dict],
    max_pattern_pages: int = 5,
    max_recipe_batch: int = 50,
) -> tuple[list[dict], list[list[dict]]]:
    """페이지를 패턴 배치(HTML 참조) + 레시피 배치(JSON만)로 분할.

    Returns:
        (pattern_pages, recipe_batches):
          pattern_pages — 첫 배치용 패턴 페이지 (다양한 layout 포함)
          recipe_batches — 나머지 레시피 배치 목록 (각 max_recipe_batch 이하)
    """
    if not recipes:
        return [], []

    # 패턴 페이지: layout 유형별 1개씩 + home/login 우선
    layout_picks: dict[str, dict] = {}
    priority_slugs = ("home", "login", "register", "dashboard", "index")
    priority_page = None

    for r in recipes:
        slug = r.get("page_slug", "")
        layout = r.get("layout", "single-column")
        if any(p in slug for p in priority_slugs) and not priority_page:
            priority_page = r
        if layout not in layout_picks:
            layout_picks[layout] = r

    pattern_pages = []
    if priority_page:
        pattern_pages.append(priority_page)
    for layout, page in layout_picks.items():
        if page not in pattern_pages:
            pattern_pages.append(page)
        if len(pattern_pages) >= max_pattern_pages:
            break

    # 나머지 → 레시피 배치
    pattern_slugs = {p["page_slug"] for p in pattern_pages}
    remaining = [r for r in recipes if r["page_slug"] not in pattern_slugs]

    recipe_batches = []
    for i in range(0, len(remaining), max_recipe_batch):
        recipe_batches.append(remaining[i:i + max_recipe_batch])

    return pattern_pages, recipe_batches


def _match_html_to_slugs(
    html_pages: list[str],
    recipes: list[dict],
) -> dict[str, str]:
    """조립된 HTML을 page_slug에 매칭 (title 태그 기반)."""
    import re as _re_match

    slug_to_html: dict[str, str] = {}
    name_to_slug = {r.get("page_name", ""): r["page_slug"] for r in recipes}
    title_to_slug = {r.get("title", ""): r["page_slug"] for r in recipes}

    for html in html_pages:
        m = _re_match.search(r"<title>(.+?)</title>", html, _re_match.IGNORECASE)
        title = m.group(1).strip() if m else ""

        matched_slug = None
        # 1차: title 정확 매칭
        if title in title_to_slug:
            matched_slug = title_to_slug[title]
        else:
            # 2차: page_name 포함 매칭
            for name, slug in name_to_slug.items():
                if name and name in title:
                    matched_slug = slug
                    break

        if matched_slug and matched_slug not in slug_to_html:
            slug_to_html[matched_slug] = html

    return slug_to_html


def _extract_batch_summary(output: str, batch_pages: list[str]) -> str:
    """배치 출력에서 라우트 + 컴포넌트명 요약 추출 (다음 배치 전달용, 최대 2K)."""
    import re as _re_sum
    lines = []

    # 라우트 테이블 행 추출 (| /path | file.tsx | 패턴)
    for m in _re_sum.finditer(r"\|[^|]*(/[a-z0-9/_-]+)[^|]*\|[^|]*([a-zA-Z0-9_/]+\.tsx)[^|]*\|", output):
        lines.append(f"  {m.group(1)} → {m.group(2)}")

    # FILE_MANIFEST 파일 목록 추출
    fm_match = _re_sum.search(r"FILE_MANIFEST\s*```json\s*(\{.*?\})\s*```", output, _re_sum.DOTALL)
    if fm_match:
        try:
            import json as _jfm
            fm = _jfm.loads(fm_match.group(1))
            for f in fm.get("files", [])[:20]:
                path = f.get("path", "") if isinstance(f, dict) else str(f)
                if path and path not in "\n".join(lines):
                    lines.append(f"  {path}")
        except (ValueError, TypeError) as exc:
            logger.debug("file_manifest_parse_failed error=%s", exc)

    summary = "이전 배치 생성 파일:\n" + "\n".join(lines[:30]) if lines else ""
    return summary[:2000]


def _merge_batch_outputs(
    pattern_output: str,
    recipe_outputs: list[str],
    all_pages: list[str],
) -> str:
    """전체 배치 출력을 단일 아티팩트로 병합.

    QA가 앞부분 15K만 보므로 구조:
      1. 제목 + 라우트 테이블 + 파일 목록 (QA 핵심 검증 대상)
      2. 코드 블록 (배치 순서대로)
      3. FILE_MANIFEST (파일 매니페스트)
    """
    import re as _re_merge
    import json as _json_merge

    all_outputs = [pattern_output or ""] + recipe_outputs

    # ── 1단계: 모든 배치에서 // FILE: 태그 추출 ──
    all_files = []
    file_routes = []  # (파일경로, 페이지명 추정)
    for output in all_outputs:
        for m in _re_merge.finditer(r"// FILE: (\S+)", output):
            fpath = m.group(1)
            if fpath not in all_files:
                all_files.append(fpath)
                # page.tsx 파일에서 라우트 추정
                if fpath.endswith("page.tsx") or fpath.endswith("page.jsx"):
                    route = fpath.replace("src/app", "").replace("/page.tsx", "").replace("/page.jsx", "")
                    if not route:
                        route = "/"
                    file_routes.append((route, fpath))

    # FILE_MANIFEST에서도 파일 추출
    for output in all_outputs:
        fm_match = _re_merge.search(
            r"FILE_MANIFEST\s*```json\s*(\{.*?\})\s*```", output, _re_merge.DOTALL
        )
        if fm_match:
            try:
                fm = _json_merge.loads(fm_match.group(1))
                for f in fm.get("files", []):
                    path = f.get("path", "") if isinstance(f, dict) else str(f)
                    if path and path not in all_files:
                        all_files.append(path)
            except (ValueError, TypeError) as exc:
                logger.debug("merge_manifest_parse_failed error=%s", exc)

    # ── 2단계: 헤더 구성 (QA가 반드시 보는 앞부분) ──
    parts = []
    parts.append("# 프론트엔드 컴포넌트 구현\n")

    # 페이지 라우트 목록 (라우트 테이블)
    parts.append(f"## 페이지 라우트 목록 (총 {len(all_pages)}페이지)\n")
    if file_routes:
        parts.append("| 라우트 | 파일 |")
        parts.append("|---|---|")
        for route, fpath in sorted(file_routes):
            parts.append(f"| `{route}` | `{fpath}` |")
        parts.append("")

    # 전체 파일 목록 요약
    parts.append(f"\n## 생성 파일 목록 (총 {len(all_files)}개)\n")
    for fpath in all_files:
        parts.append(f"- `{fpath}`")
    parts.append("")

    # ── 3단계: 코드 블록 (배치 순서대로) ──
    parts.append("\n---\n## 페이지 컴포넌트\n")

    if pattern_output:
        parts.append("### 패턴 참조 페이지\n")
        parts.append(pattern_output)

    for i, output in enumerate(recipe_outputs):
        parts.append(f"\n---\n### 배치 {i + 2} 페이지\n")
        parts.append(output)

    # ── 4단계: FILE_MANIFEST 통합 ──
    if all_files:
        manifest = {"files": [{"path": p} for p in all_files]}
        parts.append(
            f"\n\n<!-- FILE_MANIFEST {_json_merge.dumps(manifest, ensure_ascii=False)} -->"
        )

    return "\n".join(parts)


def _sample_code_files(content: str, max_total: int = 10000) -> str:
    """대형 코드 산출물에서 // FILE: 태그별 샘플 추출.

    각 파일의 첫 500자(import + Props + 주요 구조)만 추출하여
    QA AI가 전체 파일 구조를 파악할 수 있게 함.
    """
    import re as _re_sample
    parts = _re_sample.split(r"(?=// FILE: )", content)
    samples = []
    total = 0
    for part in parts:
        if not part.strip() or "// FILE:" not in part:
            continue
        sample = part[:500]
        if total + len(sample) > max_total:
            remaining = len(parts) - len(samples)
            if remaining > 0:
                samples.append(f"\n... (나머지 {remaining}개 파일 생략)")
            break
        samples.append(sample)
        total += len(sample)
    return "\n---\n".join(samples) if samples else ""


# QA FAIL 시 코드 구조 문제 → TASK 재실행 트리거 키워드
_TASK_RETRIGGER_KEYWORDS = (
    "코드 절단", "코드 중간 절단", "중간 절단",
    "페이지 누락", "대량 누락", "완전 누락",
    "구현율", "미완성", "심각한 미완성",
    "FILE_MANIFEST 누락", "FILE_MANIFEST 완전 누락",
    "코드 블록 중간 절단",
)


# ---------------------------------------------------------------------------
# Output Size Anchoring — AI 비결정성 구조적 해결
# ---------------------------------------------------------------------------

# 플랫폼별 HTML→코드 변환 배수 (경험치 기반)
# HTML은 순수 마크업이고 프레임워크 코드는 import/state/hooks/types 등이 추가되므로
# 일반적으로 입력 HTML보다 출력 코드가 더 큼
_PLATFORM_SIZE_MULTIPLIER = {
    "web_nextjs":          0.8,   # React+TSX는 HTML 대비 ~80% (JSX가 간결)
    "web_vue":             0.9,   # SFC(template+script+style)는 약간 더 큼
    "mobile_react_native": 0.6,   # StyleSheet 별도, HTML 구조와 다름
    "mobile_flutter":      0.7,   # Widget tree는 HTML보다 간결
    "mobile_swift":        0.6,   # SwiftUI modifier chain
    "mobile_kotlin":       0.7,   # Jetpack Compose
    "hybrid_rn_web":       0.7,   # RN Web은 platform-specific 추가
    "hybrid_flutter_web":  0.7,
    "desktop_electron":    0.8,   # React 기반이므로 web_nextjs와 유사
}

# 컴포넌트당 최소 바이트 (공통 인프라 파일 포함)
_MIN_CHARS_PER_COMPONENT_FILE = 800   # import+props+render+export 최소
_MIN_CHARS_PER_PAGE = 2000            # 페이지 컴포넌트 최소 (라우팅+레이아웃+컨텐츠)


def _estimate_output_size(
    input_html_chars: int,
    page_count: int,
    platform_key: str = "web_nextjs",
    has_infra: bool = True,
) -> dict:
    """입력 기반으로 기대 출력 크기를 산정.

    AI에게 구체적 크기 기대치를 제공하여 비결정성을 구조적으로 억제.

    Args:
        input_html_chars: 주입된 조립 HTML 총 문자 수
        page_count:       변환 대상 페이지 수
        platform_key:     PLATFORM_CONFIG의 key
        has_infra:        공통 인프라(globals.css, layout 등) 포함 여부

    Returns:
        {
            "min_chars":      int,  # 이 이하면 즉시 INVALID (repair 낭비 방지)
            "expected_chars": int,  # 프롬프트에 주입할 기대 크기
            "min_files":      int,  # 최소 // FILE: 태그 수
            "expected_files": int,  # 기대 파일 수
        }
    """
    multiplier = _PLATFORM_SIZE_MULTIPLIER.get(platform_key, 0.8)

    # 입력 HTML 기반 기대치
    html_based_expected = int(input_html_chars * multiplier)

    # 페이지 수 기반 기대치 (HTML 없을 때의 폴백)
    page_based_expected = page_count * _MIN_CHARS_PER_PAGE

    # 둘 중 큰 값 채택 (보수적)
    expected_chars = max(html_based_expected, page_based_expected)

    # 인프라 파일 추가 (globals.css, layout, providers 등 = ~3K)
    infra_overhead = 3000 if has_infra else 0
    expected_chars += infra_overhead

    # 최소 기준: 기대치의 30% (이 이하면 AI가 lazy output 생성한 것)
    min_chars = max(expected_chars * 3 // 10, page_count * _MIN_CHARS_PER_COMPONENT_FILE)

    # 파일 수: 페이지 + 공통 컴포넌트(헤더/푸터/사이드바 등 ~3개) + 인프라(~3개)
    common_components = min(3, max(1, page_count // 3))
    infra_files = 3 if has_infra else 0
    expected_files = page_count + common_components + infra_files
    min_files = max(page_count, 1)  # 최소한 페이지 수만큼

    return {
        "min_chars": min_chars,
        "expected_chars": expected_chars,
        "min_files": min_files,
        "expected_files": expected_files,
    }


def _build_size_anchor_block(estimate: dict, page_count: int, platform_name: str) -> str:
    """프롬프트에 주입할 출력 크기 앵커링 블록.

    AI에게 명시적 크기 기대치를 제공하여 lazy output(2.5K)과
    과잉 output(66K)을 모두 억제.
    """
    return (
        f"\n\n## ⚠️ 출력 크기 기준 (필수 준수)\n"
        f"- **페이지 수**: {page_count}개 → 각 페이지 컴포넌트 최소 {_MIN_CHARS_PER_PAGE:,}자 이상\n"
        f"- **기대 총 출력**: 약 {estimate['expected_chars']:,}자 ({platform_name} 기준)\n"
        f"- **최소 허용**: {estimate['min_chars']:,}자 미만은 품질 검증 실패 처리됨\n"
        f"- **기대 파일 수**: {estimate['expected_files']}개 이상 (`// FILE:` 태그)\n"
        f"- 각 파일은 import부터 export까지 **완전한 코드**여야 합니다\n"
        f"- 요약/스켈레톤/placeholder 코드 금지 — 실제 동작하는 전체 코드 출력\n"
    )


async def _load_batch_cache(db: Any, node_id: str) -> dict[int, str]:
    """이전 배치 실행의 성공 결과 캐시 로드 (품질 검증 포함).

    artifact_versions에서 <!-- BATCH:N STATUS:OK --> 마커가 있는
    버전을 찾아 {batch_index: content} dict로 반환.
    품질 검증 실패 캐시는 폐기 (불량 캐시 재사용 방지).
    """
    import re as _re_cache
    from engine.skills.qa.harness import _validate_batch_output

    try:
        rows = await db.fetchall(
            """SELECT storage_path FROM artifact_versions av
               JOIN artifacts a ON a.id = av.artifact_id
               WHERE a.node_id=? AND av.storage_path LIKE '%<!-- BATCH:%'
               ORDER BY av.version_num""",
            (node_id,),
        )
    except Exception as exc:
        logger.warning("batch_cache_query_failed node=%s error=%s", node_id, exc)
        return {}

    cache: dict[int, str] = {}
    for row in rows:
        content = row["storage_path"] or ""
        m = _re_cache.match(r"<!-- BATCH:(\d+) STATUS:OK -->", content)
        if m:
            batch_idx = int(m.group(1))
            # 마커 제거한 순수 콘텐츠
            clean = _re_cache.sub(r"<!-- BATCH:\d+ STATUS:OK -->\n?", "", content, count=1)
            # 품질 검증 — 불량 캐시 폐기
            if _validate_batch_output(clean):
                cache[batch_idx] = clean
            else:
                logger.warning(
                    "batch_cache_rejected batch=%d size=%d file_tags=%d",
                    batch_idx, len(clean), clean.count("// FILE:"),
                )
    return cache


async def _prepare_batch_context(
    db: Any,
    node,
    project,
    spec: dict,
    platform: dict,
    recipes: list[dict],
) -> dict:
    """Phase 1: 배치 컨텍스트 준비 (레시피 로드, 페이지 그룹화, HTML 매칭, 프롬프트 구성).

    Returns:
        dict with keys: recipes, batch_cache, slug_to_html, pattern_pages,
        recipe_batches, total_batches, all_page_names, batch0_estimate,
        infra_summary, platform_block, spec_prompt, pattern_prompt,
        engagement_id, failure_reasons, constitution_vid, art_type, max_tokens.
    """
    import json as _jb
    from engine.core.budget_enforcer import TOKEN_BUDGET
    from engine.skills.template import render
    from engine.skills.artifact.loader import _load_infra_summary
    from engine.skills.platform import _build_platform_instruction

    # 1-1. 재시도 시 이전 성공 배치 캐시 로드
    batch_cache: dict[int, str] = {}
    if node.retry_count > 0:
        batch_cache = await _load_batch_cache(db, node.id)
        if batch_cache:
            logger.info(
                "batch_cache_loaded node=%s cached_batches=%s",
                node.id, list(batch_cache.keys()),
            )

    # 2. 조립 HTML 로드 + slug 매칭
    assembly_node = await db.fetchone(
        """SELECT n.id FROM nodes n WHERE n.project_id=?
           AND n.name='페이지 조립' AND n.state='COMPLETED' LIMIT 1""",
        (node.project_id,),
    )
    slug_to_html: dict[str, str] = {}
    if assembly_node:
        art = await db.fetchone(
            "SELECT id FROM artifacts WHERE node_id=? LIMIT 1",
            (assembly_node["id"],),
        )
        if art:
            versions = await db.fetchall(
                """SELECT storage_path FROM artifact_versions
                   WHERE artifact_id=? AND storage_path LIKE '<!DOCTYPE%'
                   ORDER BY version_num""",
                (art["id"],),
            )
            html_pages = [v["storage_path"] for v in versions]
            slug_to_html = _match_html_to_slugs(html_pages, recipes)

    # 3. 배치 분할
    pattern_pages, recipe_batches = _group_pages_for_batching(recipes)
    total_batches = 1 + len(recipe_batches)
    all_page_names = [r["page_name"] for r in recipes]

    # 3-1. Output Size Anchoring
    _total_html_chars = sum(len(h) for h in slug_to_html.values())
    _batch0_html_chars = sum(
        len(slug_to_html.get(p["page_slug"], "")) for p in pattern_pages
    )
    _batch0_estimate = _estimate_output_size(
        input_html_chars=_batch0_html_chars,
        page_count=len(pattern_pages),
        platform_key=platform.get("key", "web_nextjs"),
        has_infra=True,
    )

    logger.info(
        "batched_frontend_start project=%s pages=%d pattern=%d batches=%d platform=%s "
        "total_html=%d batch0_estimate=%s",
        node.project_id, len(recipes), len(pattern_pages), total_batches, platform["key"],
        _total_html_chars, _batch0_estimate,
    )

    # 4. 공통 컨텍스트 (1회 로드)
    infra_summary = await _load_infra_summary(db, node.project_id)
    platform_block = _build_platform_instruction(platform)

    variables = {
        "name": node.name,
        "project_name": project.project_name,
        "client_name": project.client_name,
        "phase": node.phase,
        "framework": platform["framework"],
    }
    spec_prompt = render(spec["prompt"], variables) if spec and spec.get("prompt") else ""

    # 5. 배치 0 패턴 프롬프트 구성
    pattern_prompt_parts = [
        spec_prompt,
        platform_block,
        f"\n\n## 패턴 학습 배치 (전체 {len(recipes)}페이지 중 {len(pattern_pages)}개)\n"
        f"아래 HTML 페이지를 {platform['framework']} 컴포넌트로 변환하세요.\n"
        f"이후 배치에서 이 패턴을 재사용합니다.\n",
    ]

    for pp in pattern_pages:
        html = slug_to_html.get(pp["page_slug"], "")
        recipe_json = _jb.dumps(pp["data"], ensure_ascii=False, indent=1)[:3000]
        if html:
            pattern_prompt_parts.append(
                f"\n### 페이지: {pp['page_name']} ({pp['page_slug']})\n"
                f"#### HTML 참조\n```html\n{html[:8000]}\n```\n"
                f"#### 레시피\n```json\n{recipe_json}\n```\n"
            )
        else:
            pattern_prompt_parts.append(
                f"\n### 페이지: {pp['page_name']} ({pp['page_slug']})\n"
                f"#### 레시피\n```json\n{recipe_json}\n```\n"
            )

    if infra_summary:
        pattern_prompt_parts.append(infra_summary)

    pattern_prompt_parts.append(
        "\n\n## 출력 형식 (필수 — 이 형식을 정확히 따르세요)\n\n"
        "### 1단계: 페이지 라우트 목록\n"
        "출력 맨 앞에 아래 형식의 라우트 테이블을 작성하세요:\n"
        "```\n"
        "## 📋 페이지 라우트 목록\n"
        "| # | 페이지명 | 라우트 | 파일 경로 |\n"
        "|---|---------|--------|----------|\n"
        "| 1 | 메인 | / | src/pages/MainPage.tsx |\n"
        "```\n\n"
        "### 2단계: 파일별 코드 출력\n"
        "각 파일은 반드시 `// FILE:` 태그로 시작합니다:\n"
        "```\n"
        "// FILE: src/pages/MainPage.tsx\n"
        "import React from 'react';\n"
        "// ... 완전한 컴포넌트 코드 ...\n"
        "export default MainPage;\n"
        "```\n"
        "- 모든 페이지를 빠짐없이 `// FILE:` 태그로 출력\n"
        "- 코드 조각이 아닌 **완전한 파일 단위** 출력 (import부터 export까지)\n"
        "- 공통 컴포넌트(헤더, 사이드바, 레이아웃 등)도 별도 `// FILE:` 로 출력\n\n"
        "### 3단계: FILE_MANIFEST\n"
        "출력 맨 끝에 생성한 모든 파일의 매니페스트를 작성하세요:\n"
        "```json\n"
        'FILE_MANIFEST\n'
        '{\n'
        '  "files": [\n'
        '    {"path": "src/pages/MainPage.tsx", "type": "page"},\n'
        '    {"path": "src/components/Header.tsx", "type": "component"}\n'
        '  ]\n'
        '}\n'
        "```\n\n"
        "**⚠️ 위 3단계를 모두 포함하지 않은 출력은 파싱 실패로 처리됩니다.**\n"
        "이후 배치에서 이 패턴(라우트 테이블 → // FILE: 코드 → FILE_MANIFEST)을 동일하게 적용합니다.\n"
    )

    pattern_prompt_parts.append(
        _build_size_anchor_block(_batch0_estimate, len(pattern_pages), platform["framework"])
    )
    pattern_prompt = "\n".join(pattern_prompt_parts)

    # 공통: engagement_id, failure_reasons, constitution_version_id
    engagement_id = project.engagement_id
    failure_reasons = []
    if node.retry_count > 0:
        fr_row = await db.fetchone("SELECT failure_reasons FROM nodes WHERE id=?", (node.id,))
        if fr_row and fr_row.get("failure_reasons"):
            try:
                failure_reasons = json.loads(fr_row["failure_reasons"])
            except (ValueError, TypeError) as exc:
                logger.debug("failure_reasons_parse_failed node=%s error=%s", node.id[:8], exc)
    cv_row = await db.fetchone(
        "SELECT constitution_version_id FROM projects WHERE id=?", (node.project_id,)
    )
    constitution_vid = cv_row["constitution_version_id"] if cv_row else None

    art_type = spec.get("type", "code") if spec else "code"
    max_tokens = TOKEN_BUDGET.get("max_output", 16000)

    return {
        "batch_cache": batch_cache,
        "slug_to_html": slug_to_html,
        "pattern_pages": pattern_pages,
        "recipe_batches": recipe_batches,
        "total_batches": total_batches,
        "all_page_names": all_page_names,
        "batch0_estimate": _batch0_estimate,
        "infra_summary": infra_summary,
        "platform_block": platform_block,
        "spec_prompt": spec_prompt,
        "pattern_prompt": pattern_prompt,
        "engagement_id": engagement_id,
        "failure_reasons": failure_reasons,
        "constitution_vid": constitution_vid,
        "art_type": art_type,
        "max_tokens": max_tokens,
    }


async def _execute_single_batch(
    db: Any,
    node,
    project,
    batch: list[dict],
    batch_idx: int,
    cache_key: int,
    ctx: dict,
    assembler,
    model_adapter,
    budget_enforcer,
) -> str:
    """Phase 2: 단일 batch AI 호출 + 결과 검증.

    Returns:
        batch output string.
    """
    import json as _jb
    from engine.ai.context_assembler import NodeContext
    from engine.skills.artifact.saver import _save_artifact
    from engine.skills.qa.harness import _validate_batch_output

    slug_to_html = ctx["slug_to_html"]
    total_batches = ctx["total_batches"]
    spec_prompt = ctx["spec_prompt"]
    platform_block = ctx["platform_block"]
    infra_summary = ctx["infra_summary"]
    platform = ctx["platform"]
    art_type = ctx["art_type"]
    max_tokens = ctx["max_tokens"]

    # 레시피 + HTML 참조 구성 (페이지당 HTML 힌트 동반)
    _BATCH_PAGE_HTML_BUDGET = 2000
    _BATCH_HTML_CAP = 80000
    page_blocks = []
    html_total = 0
    for r in batch:
        slug = r["page_slug"]
        recipe_json = _jb.dumps(
            {"page_name": r["page_name"], "page_slug": slug,
             "layout": r["layout"],
             "placements": r["data"].get("placements", [])},
            ensure_ascii=False, indent=1,
        )
        html = slug_to_html.get(slug, "")
        block = f"#### 페이지: {r['page_name']} ({slug})\n"
        if html and html_total < _BATCH_HTML_CAP:
            budget = min(_BATCH_PAGE_HTML_BUDGET, _BATCH_HTML_CAP - html_total)
            preview = "\n".join(html.splitlines()[:40])[:budget]
            block += f"**HTML 참조** (구조 힌트 — 시각적 결과 동일하게 변환)\n```html\n{preview}\n```\n"
            html_total += len(preview)
        block += f"**레시피**\n```json\n{recipe_json}\n```\n"
        page_blocks.append(block)

    # 이전 배치 요약 (최근 2개)
    prev_summaries = ctx.get("prev_summaries", [])
    prev_ctx_str = "\n".join(prev_summaries[-2:]) if prev_summaries else ""

    batch_prompt = (
        f"{spec_prompt}\n"
        f"{platform_block}\n\n"
        f"## 레시피 기반 배치 {batch_idx + 2}/{total_batches} "
        f"({len(batch)}페이지)\n"
        f"배치 0에서 학습한 변환 패턴에 따라 아래 레시피와 HTML 참조를 "
        f"{platform['framework']} 컴포넌트로 변환하세요.\n"
        f"각 페이지의 HTML 구조를 기반으로 정확한 시각적 변환을 수행하세요.\n\n"
        f"### 페이지별 레시피 + HTML 참조\n"
        + "\n".join(page_blocks) + "\n"
    )

    if infra_summary:
        batch_prompt += infra_summary

    if prev_ctx_str:
        batch_prompt += f"\n\n### 이전 배치 결과 참조 (일관성 유지)\n{prev_ctx_str}\n"

    batch_prompt += (
        "\n\n## 출력 지시\n"
        "- 각 페이지별 완전한 컴포넌트 코드 작성\n"
        "- 배치 0의 패턴(라우팅, 임포트, 스타일링)을 정확히 따르세요\n"
        "- FILE_MANIFEST ```json {...}``` 포함 필수\n"
    )

    # 배치별 크기 앵커링
    _batch_html_chars = sum(
        len(slug_to_html.get(r["page_slug"], "")) for r in batch
    )
    _batch_estimate = _estimate_output_size(
        input_html_chars=_batch_html_chars,
        page_count=len(batch),
        platform_key=platform.get("key", "web_nextjs"),
        has_infra=False,
    )
    batch_prompt += _build_size_anchor_block(
        _batch_estimate, len(batch), platform["framework"]
    )

    batch_ctx = NodeContext(
        node_id=node.id,
        node_type=node.node_type,
        name=node.name,
        description=batch_prompt,
        phase=node.phase,
        project_id=node.project_id,
        engagement_id=ctx["engagement_id"],
        retry_count=node.retry_count,
        failure_reasons=ctx["failure_reasons"],
        assigned_model=node.assigned_model or "sonnet",
        constitution_version_id=ctx["constitution_vid"],
    )
    batch_assembly = assembler.assemble(batch_ctx, project, deltas=[])

    try:
        await budget_enforcer.pre_call_check(
            node.id, project.project_id, node.phase, batch_assembly.prompt
        )
    except Exception:
        logger.warning("batched_frontend_budget_warn batch=%d", batch_idx + 1)

    batch_resp = await model_adapter.call(
        model=node.assigned_model or "sonnet",
        system=batch_assembly.system,
        prompt=batch_assembly.prompt,
        max_tokens=max(max_tokens, 32000),
    )

    await budget_enforcer.post_call_record(
        node.id, None, project.project_id, node.phase,
        node.assigned_model or "sonnet",
        batch_resp.input_tokens, batch_resp.output_tokens,
    )

    # 배치 출력 동적 크기 검증
    if not _validate_batch_output(
        batch_resp.content,
        min_files=max(1, len(batch)),
        size_estimate=_batch_estimate,
    ):
        logger.warning(
            "batch_recipe_quality_fail batch=%d size=%d min=%d files=%d/%d",
            cache_key, len(batch_resp.content),
            _batch_estimate["min_chars"],
            batch_resp.content.count("// FILE:"), len(batch),
        )
        raise ValueError(
            f"배치 {cache_key} 품질 검증 실패: "
            f"출력 {len(batch_resp.content):,}자/기대 {_batch_estimate['expected_chars']:,}자, "
            f"// FILE: {batch_resp.content.count('// FILE:')}개/{len(batch)}개 필요"
        )

    # 성공 캐시 저장
    await _save_artifact(
        db, node,
        f"<!-- BATCH:{cache_key} STATUS:OK -->\n{batch_resp.content}",
        art_type,
    )

    logger.info(
        "batched_frontend batch=%d/%d type=recipe pages=%d html_chars=%d "
        "output=%d expected=%d input_tok=%d output_tok=%d",
        cache_key, total_batches, len(batch), html_total,
        len(batch_resp.content), _batch_estimate["expected_chars"],
        batch_resp.input_tokens, batch_resp.output_tokens,
    )

    return batch_resp.content


async def _finalize_batch_results(
    db: Any,
    node,
    pattern_output: str,
    recipe_outputs: list[str],
    batch_errors: list[tuple],
    all_page_names: list[str],
    total_batches: int,
    art_type: str,
) -> None:
    """Phase 3: batch 결과 병합 + artifact 저장 + 노드 완료."""
    from engine.skills.artifact.saver import _save_artifact

    # 전체 실패 체크
    if not pattern_output and not any(o for o in recipe_outputs if "FAILED" not in o):
        raise ValueError(
            f"프론트엔드 배치 생성 전체 실패 ({total_batches}배치): "
            + "; ".join(e for _, e in batch_errors[:3])
        )

    # 병합 + 저장
    merged = _merge_batch_outputs(pattern_output, recipe_outputs, all_page_names)
    await _save_artifact(db, node, merged, art_type)

    # 노드 완료
    await db.execute(
        "UPDATE nodes SET state='COMPLETED', completed_at=?, updated_at=? WHERE id=?",
        (_now(), _now(), node.id),
    )

    logger.info(
        "batched_frontend_complete project=%s pages=%d batches=%d errors=%d merged_chars=%d",
        node.project_id, len(all_page_names), total_batches, len(batch_errors), len(merged),
    )


async def _batched_frontend_generate(
    db: Any,
    node: "NodeSnapshot",
    project: "ProjectContext",
    spec: dict,
    platform: dict,
    assembler: "ContextAssembler",
    model_adapter: "ModelAdapter",
    budget_enforcer: "BudgetEnforcer",
) -> None:
    """대규모 프로젝트 배치 실행: 페이지 레시피 기반으로 분할 생성 후 병합.

    배치 0: 패턴 페이지 (FULL HTML + 레시피) → 변환 패턴 학습
    배치 1~N: 레시피 JSON만 → 패턴 따라 코드 생성
    """
    import json as _jb
    from engine.ai.context_assembler import NodeContext
    from engine.core.budget_enforcer import TOKEN_BUDGET
    from engine.skills.artifact.saver import _save_artifact
    from engine.skills.qa.harness import _validate_batch_output

    # 1. 전체 레시피 로드
    recipe_rows = await db.fetchall(
        "SELECT page_slug, page_name, data FROM composition_recipes WHERE project_id=? ORDER BY page_slug",
        (node.project_id,),
    )
    recipes = []
    for r in recipe_rows:
        try:
            data = _jb.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        except (ValueError, TypeError):
            data = {}
        recipes.append({
            "page_slug": r["page_slug"],
            "page_name": r["page_name"],
            "layout": data.get("layout", "single-column"),
            "data": data,
        })

    if not recipes:
        logger.warning("batched_frontend_no_recipes project=%s", node.project_id)
        return

    # Phase 1: 컨텍스트 준비
    ctx = await _prepare_batch_context(db, node, project, spec, platform, recipes)
    ctx["platform"] = platform  # _execute_single_batch에서 필요

    batch_cache = ctx["batch_cache"]
    pattern_pages = ctx["pattern_pages"]
    recipe_batches = ctx["recipe_batches"]
    total_batches = ctx["total_batches"]
    art_type = ctx["art_type"]
    max_tokens = ctx["max_tokens"]

    # Phase 2a: 배치 0 패턴 (캐시 히트 시 AI 스킵)
    if 0 in batch_cache:
        pattern_output = batch_cache[0]
        logger.info("batch_cache_hit batch=0/%d (AI 스킵)", total_batches)
    else:
        node_ctx = NodeContext(
            node_id=node.id,
            node_type=node.node_type,
            name=node.name,
            description=ctx["pattern_prompt"],
            phase=node.phase,
            project_id=node.project_id,
            engagement_id=ctx["engagement_id"],
            retry_count=node.retry_count,
            failure_reasons=ctx["failure_reasons"],
            assigned_model=node.assigned_model or "sonnet",
            constitution_version_id=ctx["constitution_vid"],
        )
        assembly = assembler.assemble(node_ctx, project, deltas=[])

        try:
            await budget_enforcer.pre_call_check(
                node.id, project.project_id, node.phase, assembly.prompt
            )
        except Exception as exc:
            logger.warning("batch0_budget_exceeded error=%s — proceeding anyway (pattern batch)", exc)

        _batch0_estimate = ctx["batch0_estimate"]
        _batch0_min_files = max(1, len(pattern_pages))
        pattern_output = ""
        for _b0_attempt in range(2):
            pattern_resp = await model_adapter.call(
                model=node.assigned_model or "sonnet",
                system=assembly.system,
                prompt=assembly.prompt,
                max_tokens=max_tokens,
            )
            pattern_output = pattern_resp.content

            await budget_enforcer.post_call_record(
                node.id, None, project.project_id, node.phase,
                node.assigned_model or "sonnet",
                pattern_resp.input_tokens, pattern_resp.output_tokens,
            )

            if _validate_batch_output(
                pattern_output,
                min_files=_batch0_min_files,
                size_estimate=_batch0_estimate,
            ):
                break
            logger.warning(
                "batch0_quality_fail attempt=%d size=%d min=%d file_tags=%d/%d — %s",
                _b0_attempt + 1, len(pattern_output),
                _batch0_estimate["min_chars"],
                pattern_output.count("// FILE:"), _batch0_min_files,
                "retrying" if _b0_attempt == 0 else "giving up",
            )
        else:
            raise ValueError(
                f"배치 0 패턴 생성 실패 (2회 시도, "
                f"출력 {len(pattern_output):,}자/기대 {_batch0_estimate['expected_chars']:,}자, "
                f"// FILE: {pattern_output.count('// FILE:')}개/{_batch0_min_files}개 필요)"
            )

        await _save_artifact(
            db, node,
            f"<!-- BATCH:0 STATUS:OK -->\n{pattern_output}",
            art_type,
        )

        logger.info(
            "batched_frontend batch=0/%d type=pattern pages=%d input_tok=%d output_tok=%d",
            total_batches, len(pattern_pages),
            pattern_resp.input_tokens, pattern_resp.output_tokens,
        )

    # Phase 2b: 배치 1~N 레시피 기반 생성
    recipe_outputs = []
    prev_summaries = [_extract_batch_summary(pattern_output, [p["page_name"] for p in pattern_pages])]
    batch_errors = []
    ctx["prev_summaries"] = prev_summaries

    for batch_idx, batch in enumerate(recipe_batches):
        cache_key = batch_idx + 1

        if cache_key in batch_cache:
            recipe_outputs.append(batch_cache[cache_key])
            prev_summaries.append(
                _extract_batch_summary(batch_cache[cache_key], [r["page_name"] for r in batch])
            )
            logger.info("batch_cache_hit batch=%d/%d (AI 스킵)", cache_key, total_batches)
            continue

        try:
            output = await _execute_single_batch(
                db, node, project, batch, batch_idx, cache_key, ctx,
                assembler, model_adapter, budget_enforcer,
            )
            recipe_outputs.append(output)
            prev_summaries.append(
                _extract_batch_summary(output, [r["page_name"] for r in batch])
            )
        except Exception as exc:
            logger.error(
                "batched_frontend_batch_failed batch=%d error=%s",
                cache_key, str(exc),
            )
            batch_errors.append((batch_idx, str(exc)[:300]))
            recipe_outputs.append(
                f"<!-- BATCH {cache_key} FAILED: {str(exc)[:200]} -->\n"
                f"/* 실패 페이지: {', '.join(r['page_name'] for r in batch[:5])} "
                f"{'...' if len(batch) > 5 else ''} */\n"
            )
            await _save_artifact(
                db, node,
                f"<!-- BATCH:{cache_key} STATUS:FAIL -->\n{str(exc)[:500]}",
                art_type,
            )

    # Phase 3: 병합 + 저장
    await _finalize_batch_results(
        db, node, pattern_output, recipe_outputs, batch_errors,
        ctx["all_page_names"], total_batches, art_type,
    )
