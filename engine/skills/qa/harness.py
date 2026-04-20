from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _harness_validate_programmatic(
    content: str,
    task_desc: dict,
    recipe_rows: list[dict],
) -> dict:
    """프로그래매틱 코드 산출물의 구조적 무결성을 코드로 검증.

    검증 항목:
      1. // FILE: 태그 수 ≥ 기대 파일 수
      2. export default 존재 (페이지 컴포넌트)
      3. JSX 구문 검증 (괄호/태그 밸런스)
      4. import 문 존재 (React 최소)
      5. interface/type 정의 존재
      6. placement 반영 (레시피 컴포넌트가 코드에 존재)

    Returns:
        {"pass": bool, "checks": [...], "failures": [...]}
    """
    import re as _re

    checks: list[dict] = []
    failures: list[str] = []

    # ── 기본 정보 ──
    files_generated = task_desc.get("_files_generated", [])
    page_slugs = task_desc.get("_page_slugs_generated", task_desc.get("page_slugs", []))

    # ── 레시피 산출물은 JSON이므로 코드 검증 건너뜀 ──
    if task_desc.get("_recipe_count") is not None:
        recipe_count = task_desc.get("_recipe_count", 0)
        c_pass = recipe_count > 0
        checks.append({"name": "recipe_count", "pass": c_pass, "count": recipe_count})
        if not c_pass:
            failures.append("레시피 0개 생성됨")
        # 레시피 slug 검증
        slugs = task_desc.get("_recipe_slugs", [])
        checks.append({"name": "recipe_slugs", "pass": len(slugs) > 0, "count": len(slugs)})
        return {"pass": len(failures) == 0, "checks": checks, "failures": failures, "structural_failures": failures}

    # ── Check 1: // FILE: 또는 /* FILE: 태그 수 (JS/CSS 모두 카운트) ──
    file_tags = _re.findall(r'(?://|/\*)\s*FILE:\s*(\S+)', content)
    expected_files = len(files_generated) if files_generated else 1
    c1_pass = len(file_tags) >= expected_files
    checks.append({
        "name": "file_tags",
        "pass": c1_pass,
        "expected": expected_files,
        "actual": len(file_tags),
    })
    if not c1_pass:
        failures.append(f"// FILE: 태그 {len(file_tags)}개 (기대 {expected_files}개)")

    # ── 파일별 검증 (// FILE: 블록 단위로 분할) ──
    file_blocks = _split_by_file_tag(content)

    total_exports = 0
    total_imports = 0
    total_interfaces = 0
    jsx_issues: list[str] = []

    for filepath, block in file_blocks.items():
        # .tsx/.jsx 파일만 깊은 검증
        is_component = filepath.endswith((".tsx", ".jsx"))
        is_ts = filepath.endswith((".ts", ".tsx"))
        is_css = filepath.endswith(".css")
        is_prisma = filepath.endswith(".prisma")
        is_sql = filepath.endswith(".sql")

        # ── Check 2: export default (컴포넌트 파일) ──
        if is_component:
            has_export = (
                "export default" in block
                or "export {" in block
                or "module.exports" in block
            )
            if has_export:
                total_exports += 1

        # ── Check 3: JSX 괄호/태그 밸런스 ──
        if is_component:
            jsx_err = _check_jsx_balance(block)
            if jsx_err:
                jsx_issues.append(f"{filepath}: {jsx_err}")

        # ── Check 4: import 문 ──
        if is_ts or is_component:
            import_count = len(_re.findall(r'^import\s', block, _re.MULTILINE))
            if import_count > 0:
                total_imports += 1

        # ── Check 5: interface/type 정의 ──
        if is_ts or is_component:
            intf_count = len(_re.findall(r'^(?:export\s+)?(?:interface|type)\s+\w+', block, _re.MULTILINE))
            total_interfaces += intf_count

    # Check 2 결과
    component_files = [f for f in file_blocks if f.endswith((".tsx", ".jsx"))]
    c2_pass = total_exports >= len(component_files) if component_files else True
    checks.append({
        "name": "export_default",
        "pass": c2_pass,
        "expected": len(component_files),
        "actual": total_exports,
    })
    if not c2_pass:
        failures.append(f"export default 누락: {total_exports}/{len(component_files)} 컴포넌트")

    # Check 3 결과
    c3_pass = len(jsx_issues) == 0
    checks.append({
        "name": "jsx_balance",
        "pass": c3_pass,
        "issues": jsx_issues[:5],
    })
    if not c3_pass:
        failures.append(f"JSX 구문 오류 {len(jsx_issues)}건: {jsx_issues[0]}")

    # Check 4 결과
    ts_files = [f for f in file_blocks if f.endswith((".ts", ".tsx"))]
    c4_pass = total_imports >= min(len(ts_files), 1)
    checks.append({
        "name": "imports_exist",
        "pass": c4_pass,
        "files_with_imports": total_imports,
        "ts_files": len(ts_files),
    })
    if not c4_pass:
        failures.append(f"import 문 없음: {total_imports}/{len(ts_files)} TS 파일")

    # Check 5 결과
    # 페이지 컴포넌트가 있으면 최소 1개 interface 기대
    c5_pass = total_interfaces >= 1 if component_files else True
    checks.append({
        "name": "interface_defined",
        "pass": c5_pass,
        "count": total_interfaces,
    })
    if not c5_pass:
        failures.append("TypeScript interface/type 정의 없음")

    # ── Check 6: placement 반영 (레시피 컴포넌트 → 코드 내 존재) ──
    if recipe_rows and page_slugs:
        missing_placements = _check_placement_coverage(content, recipe_rows, page_slugs)
        c6_pass = len(missing_placements) == 0
        checks.append({
            "name": "placement_coverage",
            "pass": c6_pass,
            "missing": missing_placements[:10],
        })
        if not c6_pass:
            failures.append(
                f"레시피 컴포넌트 미반영 {len(missing_placements)}건: "
                + ", ".join(missing_placements[:3])
            )
    else:
        checks.append({"name": "placement_coverage", "pass": True, "skipped": True})

    # ── 최종 판정 ──
    all_pass = all(c["pass"] for c in checks)

    return {
        "pass": all_pass,
        "checks": checks,
        "failures": failures,
    }


def _split_by_file_tag(content: str) -> dict[str, str]:
    """// FILE: 태그로 콘텐츠를 파일별 블록으로 분할."""
    import re as _re
    blocks: dict[str, str] = {}
    parts = _re.split(r'^(?://|/\*)\s*FILE:\s*(\S+)', content, flags=_re.MULTILINE)
    # parts: ['before', 'path1', 'code1', 'path2', 'code2', ...]
    i = 1
    while i < len(parts) - 1:
        filepath = parts[i].strip()
        code = parts[i + 1]
        blocks[filepath] = code
        i += 2
    return blocks


def _check_jsx_balance(code: str) -> str:
    """JSX 괄호 및 태그 밸런스 검증. 에러 시 설명 문자열, 정상이면 빈 문자열."""
    # 괄호 밸런스: (), {}, []
    stack = []
    pairs = {"(": ")", "{": "}", "[": "]"}
    closers = set(pairs.values())
    in_string = False
    string_char = ""
    in_template = False

    for i, ch in enumerate(code):
        # 문자열 안은 스킵
        if in_string:
            if ch == string_char and (i == 0 or code[i - 1] != "\\"):
                in_string = False
            continue
        if ch in ('"', "'", "`"):
            in_string = True
            string_char = ch
            continue
        # 한줄 주석 스킵
        if ch == "/" and i + 1 < len(code) and code[i + 1] == "/":
            break  # 이 줄의 나머지는 주석

        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in closers:
            if not stack:
                return f"닫는 '{ch}' 에 대응하는 여는 괄호 없음 (pos {i})"
            expected = stack.pop()
            if ch != expected:
                return f"'{ch}' 불일치 (기대 '{expected}', pos {i})"

    # 라인 단위로 처리했으므로 stack 잔여는 전체에서 체크
    # (멀티라인 괄호는 허용 — 최종 전체 코드에서 체크)
    # 여기서는 심각한 불일치만 잡음
    if len(stack) > 10:
        return f"닫히지 않은 괄호 {len(stack)}개 (심각한 불일치)"

    # JSX 태그 밸런스: <Comp> ... </Comp> — self-closing 제외
    import re as _re
    open_tags = _re.findall(r'<([A-Z]\w+)(?:\s|>)', code)
    close_tags = _re.findall(r'</([A-Z]\w+)>', code)
    self_closing = _re.findall(r'<([A-Z]\w+)\s[^>]*/>', code)

    # self-closing은 open에서 제거
    open_count: dict[str, int] = {}
    for t in open_tags:
        open_count[t] = open_count.get(t, 0) + 1
    for t in self_closing:
        open_count[t] = open_count.get(t, 0) - 1

    close_count: dict[str, int] = {}
    for t in close_tags:
        close_count[t] = close_count.get(t, 0) + 1

    for tag, cnt in open_count.items():
        if cnt > 0 and close_count.get(tag, 0) < cnt:
            diff = cnt - close_count.get(tag, 0)
            # Fragment (<> </>) 나 map 내부는 허용 범위
            if diff > 2:
                return f"<{tag}> 닫는 태그 부족 ({diff}개)"

    return ""


def _check_placement_coverage(
    content: str,
    recipe_rows: list[dict],
    page_slugs: list[str],
) -> list[str]:
    """레시피의 컴포넌트가 코드에 반영되었는지 확인. 누락 목록 반환."""
    missing = []

    for row in recipe_rows:
        slug = row.get("page_slug", "")
        if page_slugs and slug not in page_slugs:
            continue

        data = row.get("data")
        if not data:
            continue
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                continue

        placements = data.get("placements", [])
        for p in placements:
            comp_name = p.get("component_name", "")
            if not comp_name:
                continue
            # UX 필수 컴포넌트는 스킵 (LoadingIndicator 등은 코드에 직접 참조)
            if comp_name in (
                "loading_indicator", "error_boundary", "empty_state",
                "toast_container", "modal_container",
            ):
                continue
            # PascalCase 변환
            pascal = "".join(w.capitalize() for w in comp_name.replace("-", "_").split("_"))
            # 코드에서 <PascalName 또는 PascalName( 패턴 검색
            if f"<{pascal}" not in content and f"{pascal}(" not in content:
                missing.append(f"{slug}/{comp_name}")

    return missing


# ============================================================
# Harness QA — AI-generated 코드 구조 검증 (토큰 0)
# ============================================================

def _harness_validate_ai_code(
    content: str,
    task_name: str,
    spec: dict | None,
) -> dict:
    """AI가 생성한 코드 산출물의 구조적 무결성 검증.

    Returns:
        {
            "pass": bool,           # 전체 통과
            "structural_failures": [...],  # 구조 결함 (있으면 AI 스킵 + TASK 재실행)
            "checks": [...],
        }
    """
    import re as _re

    checks: list[dict] = []
    structural_failures: list[str] = []

    # ── 1. 최소 크기 ──
    c1_pass = len(content) >= 500
    checks.append({"name": "min_size", "pass": c1_pass, "size": len(content)})
    if not c1_pass:
        structural_failures.append(f"코드 크기 부족: {len(content)}자 (최소 500자)")

    # ── 2. // FILE: 또는 /* FILE: 태그 존재 ──
    file_count = len(_re.findall(r'(?://|/\*)\s*FILE:', content))
    c2_pass = file_count >= 1
    checks.append({"name": "file_tags", "pass": c2_pass, "count": file_count})
    if not c2_pass:
        structural_failures.append(f"// FILE: 태그 없음 (코드 구분 불가)")

    # ── 3. 코드 중간 시작 감지 ──
    first_line = ""
    for line in content.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    valid_starts = ("//", "import", "export", "'use", '"use', "#", "##", "```", "/*", "/**", "<!-", "generator", "datasource", "model", "CREATE", "--")
    c3_pass = not first_line or any(first_line.startswith(s) for s in valid_starts)
    checks.append({"name": "valid_start", "pass": c3_pass, "first_line": first_line[:80]})
    if not c3_pass:
        structural_failures.append(f"코드 중간 시작 감지: '{first_line[:60]}'")

    # ── 4. 코드 절단 감지 (마지막 블록이 불완전) ──
    file_blocks = _split_by_file_tag(content)
    truncation_suspects = []
    for filepath, block in file_blocks.items():
        stripped = block.rstrip()
        if not stripped:
            continue
        last_line = stripped.splitlines()[-1].strip() if stripped.splitlines() else ""
        # 파일이 }나 ;나 빈 줄로 끝나야 함 (정상 코드 종료)
        if filepath.endswith((".ts", ".tsx", ".js", ".jsx")):
            if last_line and not any(last_line.endswith(e) for e in ("}", ");", ";", "*/", "`", "'", '"', "export {};", "/>", ">")):
                truncation_suspects.append(filepath)
    c4_pass = len(truncation_suspects) == 0
    checks.append({"name": "no_truncation", "pass": c4_pass, "suspects": truncation_suspects[:5]})
    if not c4_pass:
        structural_failures.append(f"코드 절단 의심: {', '.join(truncation_suspects[:3])}")

    # ── 5. export 존재 (TS/TSX 파일) ──
    ts_files = [f for f in file_blocks if f.endswith((".ts", ".tsx"))]
    files_with_export = sum(
        1 for f in ts_files
        if "export " in file_blocks[f]
    )
    c5_pass = files_with_export >= len(ts_files) * 0.5 if ts_files else True
    checks.append({"name": "exports", "pass": c5_pass, "with_export": files_with_export, "total_ts": len(ts_files)})
    if not c5_pass:
        structural_failures.append(f"export 부족: {files_with_export}/{len(ts_files)} TS 파일")

    # ── 6. import 존재 ──
    files_with_import = sum(
        1 for f in ts_files
        if _re.search(r'^import\s', file_blocks[f], _re.MULTILINE)
    )
    c6_pass = files_with_import >= min(len(ts_files), 1) if ts_files else True
    checks.append({"name": "imports", "pass": c6_pass, "with_import": files_with_import})
    if not c6_pass:
        structural_failures.append(f"import 없음: {files_with_import}/{len(ts_files)} TS 파일")

    # ── 7. 컴포넌트 stub 감지 — <div {...props} /> 만 반환하는 빈 껍데기 컴포넌트 ──
    stub_files = []
    for filepath, block in file_blocks.items():
        if not filepath.endswith((".tsx", ".jsx")):
            continue
        # 컴포넌트 파일 (page.tsx, layout.tsx 제외)
        if filepath.endswith("page.tsx") or filepath.endswith("layout.tsx"):
            continue
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith("//") and not l.strip().startswith("import")]
        # stub 패턴: 함수 본문이 10줄 미만이고 <div {...props} /> 또는 <div className 만 반환
        content_lines = [l for l in lines if not l.startswith("'use") and not l.startswith("export")]
        if len(content_lines) <= 5 and any("...props" in l or "{...props}" in l for l in lines):
            stub_files.append(filepath)
    c7_pass = len(stub_files) == 0
    checks.append({"name": "no_stub_components", "pass": c7_pass, "stubs": stub_files[:10]})
    if not c7_pass:
        structural_failures.append(
            f"빈 stub 컴포넌트 {len(stub_files)}개 감지 (UI 미구현): {', '.join(stub_files[:5])}"
        )

    all_pass = len(structural_failures) == 0
    if all_pass:
        logger.info("harness_ai_code_pass checks=%d size=%d", len(checks), len(content))
    else:
        logger.warning(
            "harness_ai_code_fail failures=%d reasons=%s",
            len(structural_failures), [f[:80] for f in structural_failures[:3]],
        )
    return {
        "pass": all_pass,
        "structural_failures": structural_failures,
        "checks": checks,
    }


# ============================================================
# Harness QA — 문서형 산출물 구조 검증 (토큰 0)
# ============================================================

def _harness_validate_document(
    content: str,
    task_name: str,
    spec: dict | None,
) -> dict:
    """AI가 생성한 문서(마크다운) 산출물의 구조적 무결성 검증.

    검증 항목:
      1. 최소 크기 (빈 문서 방지)
      2. 마크다운 헤딩 존재 (구조화)
      3. 필수 섹션 존재 (spec에 정의된 경우)
      4. TODO/TBD/미정 금지어 부재
      5. 테이블 구조 (spec에 테이블 필수인 경우)
      6. SELF_CHECK 메타데이터 존재

    Returns:
        {
            "pass": bool,
            "structural_failures": [...],
            "needs_semantic": bool,    # True면 AI 의미 검증 필요
            "checks": [...],
        }
    """
    import re as _re

    checks: list[dict] = []
    structural_failures: list[str] = []
    needs_semantic = False

    # spec에서 검증 규칙 추출
    validation = spec.get("validation", {}) if spec else {}
    structural_rules = validation.get("structural", {}) if isinstance(validation, dict) else {}
    required_sections = structural_rules.get("required_sections", []) if isinstance(structural_rules, dict) else []
    requires_table = structural_rules.get("requires_table", False) if isinstance(structural_rules, dict) else False

    # ── 1. 최소 크기 ──
    min_chars = 300
    c1_pass = len(content) >= min_chars
    checks.append({"name": "min_size", "pass": c1_pass, "size": len(content), "min": min_chars})
    if not c1_pass:
        structural_failures.append(f"문서 크기 부족: {len(content)}자 (최소 {min_chars}자)")

    # ── 2. 마크다운 헤딩 존재 ──
    headings = _re.findall(r'^#{1,4}\s+.+', content, _re.MULTILINE)
    c2_pass = len(headings) >= 1
    checks.append({"name": "headings", "pass": c2_pass, "count": len(headings)})
    if not c2_pass:
        structural_failures.append("마크다운 헤딩(#) 없음 — 구조화되지 않은 문서")

    # ── 3. 필수 섹션 존재 ──
    if required_sections:
        heading_text = " ".join(h.lstrip("#").strip().lower() for h in headings)
        missing_sections = []
        for section in required_sections:
            # 섹션명이 헤딩에 포함되는지 퍼지 매칭
            section_lower = section.lower()
            if section_lower not in heading_text and section_lower.replace(" ", "") not in heading_text.replace(" ", ""):
                missing_sections.append(section)
        c3_pass = len(missing_sections) == 0
        checks.append({"name": "required_sections", "pass": c3_pass, "missing": missing_sections})
        if not c3_pass:
            structural_failures.append(f"필수 섹션 누락: {', '.join(missing_sections[:5])}")
    else:
        checks.append({"name": "required_sections", "pass": True, "skipped": True})

    # ── 4. TODO/TBD/미정 금지어 검사 + 자동수정 (harness_auto_fix) ──
    # finditer로 각 match의 위치·문맥 수집.
    # 감지 시 harness_auto_fix를 먼저 시도 → 5건 이하이고 safe region 외부면
    # 치환 적용. 치환 결과는 result["auto_fixed_content"]로 반환되어 caller가
    # artifact_versions 업데이트해야 함.
    # Korean-aware boundary: \b는 한글 접촉(TBD로, TBD은) 케이스를 놓침.
    # 대신 ASCII alphanumeric 인접만 제외 — Korean char 인접은 매치 허용.
    # 단독 2~3글자 한국어 placeholder ('미정', '미완성')은 복합어(미정산·미정의·
    # 미완성품) 안에서 false positive 빈발 → 제외. 'TODO/TBD/FIXME'와 multi-word
    # 한국어('작성 예정', '추후 작성')만 유지.
    _forbidden_re = _re.compile(
        r'(?<![a-zA-Z0-9_])(TODO|TBD|FIXME|추후\s*작성|작성\s*예정|추후\s*결정)(?![a-zA-Z0-9_])',
        _re.IGNORECASE,
    )
    matches = list(_forbidden_re.finditer(content))

    # 자동수정 시도 (감지 시에만)
    auto_fix_applied = False
    auto_fix_count = 0
    auto_fixed_content: str | None = None
    auto_fix_details: list[str] = []
    if matches:
        try:
            from engine.skills.qa.harness_auto_fix import try_auto_fix_forbidden_words
            _fix_result = try_auto_fix_forbidden_words(content)
            if _fix_result.applied:
                auto_fix_applied = True
                auto_fix_count = _fix_result.count
                auto_fixed_content = _fix_result.new_content
                auto_fix_details = _fix_result.details
                # 치환 후 남아있는 금지어 재검사 (safe region에 있는 것만 남음)
                remaining = list(_forbidden_re.finditer(auto_fixed_content))
                # safe 영역 외 잔존은 없어야 정상. 그래도 안전 재검사.
                matches = remaining
        except Exception as _afx_err:
            logger.debug("harness_auto_fix_skip: %s", _afx_err)

    c4_pass = len(matches) == 0
    found_words = [m.group() for m in matches[:10]]
    contexts_full: list[str] = []
    for m in matches[:5]:
        ctx = (auto_fixed_content or content)[max(0, m.start() - 30):m.end() + 30].replace("\n", " ").strip()
        contexts_full.append(ctx)
    checks.append({
        "name": "no_todo",
        "pass": c4_pass,
        "found": found_words,
        "contexts": contexts_full,
        "auto_fix_applied": auto_fix_applied,
        "auto_fix_count": auto_fix_count,
        "auto_fix_details": auto_fix_details,
    })
    if not c4_pass:
        sample_ctx = []
        for m in matches[:3]:
            ctx = (auto_fixed_content or content)[max(0, m.start() - 30):m.end() + 30].replace("\n", " ").strip()
            sample_ctx.append(f"'{m.group()}' at '...{ctx}...'")
        structural_failures.append(
            f"금지어 {len(matches)}건 발견: " + " | ".join(sample_ctx)
        )

    # ── 5. 테이블 구조 (필수인 경우) ──
    table_rows = _re.findall(r'^\|.+\|', content, _re.MULTILINE)
    # 헤더 구분선 제외
    data_rows = [r for r in table_rows if not _re.match(r'^\|\s*[-:]+', r)]
    if requires_table:
        c5_pass = len(data_rows) >= 2  # 헤더 + 최소 1 데이터행
        checks.append({"name": "table_required", "pass": c5_pass, "rows": len(data_rows)})
        if not c5_pass:
            structural_failures.append(f"테이블 필수이나 데이터 행 {len(data_rows)}개 (최소 2행)")
    else:
        checks.append({"name": "table_required", "pass": True, "skipped": True})

    # ── 6. SELF_CHECK 메타데이터 ──
    has_self_check = "SELF_CHECK" in content
    checks.append({"name": "self_check", "pass": has_self_check})
    # SELF_CHECK 누락은 경고 수준 — 구조 실패로 잡지 않음
    if not has_self_check:
        needs_semantic = True  # AI가 자가검증 여부 확인 필요할 수 있음

    # ── 7. 코드 블록 완결성 (열고 안 닫은 ``` 감지) ──
    backtick_count = content.count("```")
    c7_pass = backtick_count % 2 == 0
    checks.append({"name": "code_block_balance", "pass": c7_pass, "count": backtick_count})
    if not c7_pass:
        structural_failures.append(f"코드 블록 ``` 짝 불일치 ({backtick_count}개 — 홀수)")

    # ── 최종 판정 ──
    all_struct_pass = len(structural_failures) == 0

    # 구조 통과인데 SELF_CHECK 없으면 → 의미 검증은 AI에 맡길 수 있음
    # 하지만 기본적으로 구조 통과 = PASS (needs_semantic은 힌트)
    overall_pass = all_struct_pass

    result = {
        "pass": overall_pass,
        "structural_failures": structural_failures,
        "needs_semantic": needs_semantic and all_struct_pass,
        "checks": checks,
    }
    # Harness auto-fix가 적용됐으면 치환된 content를 caller에 전달.
    # caller(executor.py)는 이 content로 artifact_versions 업데이트 필요.
    if auto_fix_applied and auto_fixed_content is not None:
        result["auto_fixed_content"] = auto_fixed_content
        result["auto_fix_count"] = auto_fix_count
        result["auto_fix_details"] = auto_fix_details

    # S1-6: 문서 검증 결과 요약 로그 (이전엔 0건 → 디버깅 어려움)
    if overall_pass:
        logger.info(
            "harness_document_pass task=%s size=%d tables=%d auto_fixed=%s",
            task_name[:30] if task_name else "?",
            len(content),
            sum(1 for ln in content.split("\n") if ln.strip().startswith("|")),
            auto_fix_count if auto_fix_applied else 0,
        )
    else:
        logger.warning(
            "harness_document_fail task=%s failures=%d reasons=%s",
            task_name[:30] if task_name else "?",
            len(structural_failures),
            [f[:80] for f in structural_failures[:3]],
        )
    return result


def _validate_batch_output(
    content: str,
    min_files: int = 1,
    size_estimate: dict | None = None,
) -> bool:
    """배치 출력 품질 검증 (동적 크기 기준 지원).

    AI 비결정성 대응: 코드 조각, 형식 미준수, 중간 절단, 크기 부족을 감지.

    Args:
        content:       AI 출력 텍스트
        min_files:     최소 // FILE: 태그 수
        size_estimate: _estimate_output_size 결과 (None이면 정적 기준 사용)

    Returns: True면 품질 OK, False면 폐기 대상.
    """
    if not content:
        return False

    # 동적 크기 기준 (size_estimate 제공 시)
    effective_min_chars = 1000  # 정적 기본값
    if size_estimate:
        effective_min_chars = size_estimate["min_chars"]
        min_files = max(min_files, size_estimate.get("min_files", 1))

    if len(content) < effective_min_chars:
        return False

    # // FILE: 태그 최소 수량
    file_count = content.count("// FILE:")
    if file_count < min_files:
        return False
    # 코드 중간 시작 감지: 첫 비공백 줄이 유효한 시작인지
    first_line = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    valid_starts = ("//", "import", "export", "'use", '"use', "#", "##", "```", "/*", "/**", "<!-")
    if first_line and not any(first_line.startswith(s) for s in valid_starts):
        return False
    return True


# ============================================================
# Harness QA — Interaction Verification (토큰 0, 정적 분석)
# ============================================================

# 인터랙티브 컴포넌트 → 필요한 useState 패턴 매핑
_INTERACTIVE_COMPONENTS: dict[str, list[str]] = {
    "SearchBar": ["search", "query", "keyword"],
    "TabBar": ["tab", "activeTab", "selectedTab"],
    "Pagination": ["page", "currentPage"],
    "DataTable": ["data", "sort", "sortField", "sortOrder", "items"],
    "CalendarWidget": ["date", "selectedDate", "calendar"],
    "CareRatingForm": ["rating", "form", "score"],
}

# 이벤트 핸들러 props → 컴포넌트 매핑
_HANDLER_PROPS: dict[str, list[str]] = {
    "onSearch": ["SearchBar"],
    "onTabChange": ["TabBar"],
    "onPageChange": ["Pagination"],
    "onSort": ["DataTable"],
    "onSubmit": ["CareRatingForm"],
    "onChange": ["SearchBar", "CalendarWidget", "CareRatingForm"],
    "onSelect": ["CalendarWidget", "TabBar"],
    "onFilter": ["DataTable"],
    "onRowClick": ["DataTable"],
}


def _harness_validate_interactivity(
    content: str,
    task_name: str,
    spec: dict | None,
) -> dict:
    """AI 생성 코드의 인터랙티비티 패턴 정적 분석 검증.

    검증 항목:
      1. useState 존재 — 인터랙티브 컴포넌트 사용 시 대응 useState 필수
      2. 핸들러 와이어링 — onSearch/onTabChange 등 핸들러 prop 전달 확인
      3. 데드 핸들러 — 정의되었으나 컴포넌트에 전달되지 않는 핸들러
      4. 목 데이터 — DataTable 사용 시 최소 3개 항목의 데이터 배열
      5. 'use client' 디렉티브 — useState 사용 시 필수

    Returns:
        {"pass": bool, "structural_failures": [...], "checks": [...]}
    """
    import re as _re

    checks: list[dict] = []
    structural_failures: list[str] = []

    file_blocks = _split_by_file_tag(content)

    # page.tsx 파일만 검증 대상
    page_files = {
        fp: block for fp, block in file_blocks.items()
        if fp.endswith("page.tsx") or fp.endswith("page.jsx")
    }

    if not page_files:
        # 페이지 파일 없으면 스킵
        return {"pass": True, "structural_failures": [], "checks": [
            {"name": "interactivity", "pass": True, "skipped": True, "reason": "no page files"}
        ]}

    for filepath, block in page_files.items():

        # ── 어떤 인터랙티브 컴포넌트가 사용되는지 탐지 ──
        used_components: list[str] = []
        for comp_name in _INTERACTIVE_COMPONENTS:
            if _re.search(rf'<{comp_name}[\s/>]', block):
                used_components.append(comp_name)

        if not used_components:
            checks.append({
                "name": f"interactivity:{filepath}",
                "pass": True,
                "skipped": True,
                "reason": "no interactive components",
            })
            continue

        # ── Check 1: useState 존재 ──
        usestate_matches = _re.findall(
            r'useState\w*[<(]\s*|const\s+\[(\w+)',
            block,
        )
        usestate_vars = set()
        for m in _re.findall(r'const\s+\[(\w+)', block):
            usestate_vars.add(m.lower())

        missing_state: list[str] = []
        for comp in used_components:
            expected_patterns = _INTERACTIVE_COMPONENTS[comp]
            has_state = any(
                any(pat in var for pat in expected_patterns)
                for var in usestate_vars
            )
            # useState가 하나라도 있으면 OK (유연 매칭)
            if not has_state and "useState" not in block:
                missing_state.append(comp)

        c1_pass = len(missing_state) == 0
        checks.append({
            "name": f"useState_presence:{filepath}",
            "pass": c1_pass,
            "used_components": used_components,
            "missing_state_for": missing_state,
        })
        if not c1_pass:
            structural_failures.append(
                f"{filepath}: useState 누락 — {', '.join(missing_state)} 컴포넌트 사용 중"
            )

        # ── Check 2: 핸들러 와이어링 ──
        missing_handlers: list[str] = []
        for handler_prop, target_comps in _HANDLER_PROPS.items():
            # 이 페이지에서 해당 핸들러가 필요한 컴포넌트를 사용하는가?
            needs_handler = any(c in used_components for c in target_comps)
            if not needs_handler:
                continue
            # 핸들러 prop이 JSX에 전달되는지 확인
            # 패턴: <Component ... onSearch={handler} ...>
            prop_passed = _re.search(
                rf'{handler_prop}\s*=\s*\{{[^}}]+\}}',
                block,
            )
            # 인라인 핸들러도 허용: onSearch={() => ...}
            if not prop_passed:
                missing_handlers.append(handler_prop)

        c2_pass = len(missing_handlers) == 0
        checks.append({
            "name": f"handler_wiring:{filepath}",
            "pass": c2_pass,
            "missing_handlers": missing_handlers,
        })
        if not c2_pass:
            structural_failures.append(
                f"{filepath}: 핸들러 미전달 — {', '.join(missing_handlers)}"
            )

        # ── Check 3: 데드 핸들러 ──
        # 함수 정의: const handleXxx = ... 또는 function handleXxx
        defined_handlers = set(_re.findall(
            r'(?:const|function)\s+(handle\w+)',
            block,
        ))
        # JSX 내에서 참조: ={handleXxx} 또는 ={handleXxx(
        referenced_handlers = set(_re.findall(
            r'\{\s*(handle\w+)[\s(}]',
            block,
        ))
        dead_handlers = defined_handlers - referenced_handlers
        c3_pass = len(dead_handlers) == 0
        checks.append({
            "name": f"no_dead_handlers:{filepath}",
            "pass": c3_pass,
            "dead": list(dead_handlers)[:10],
        })
        if not c3_pass:
            structural_failures.append(
                f"{filepath}: 데드 핸들러 — {', '.join(list(dead_handlers)[:5])}"
            )

        # ── Check 4: DataTable 목 데이터 ──
        if "DataTable" in used_components:
            # 배열 리터럴 찾기: [..., ..., ...]
            array_literals = _re.findall(r'\[\s*\{[^]]*\}\s*(?:,\s*\{[^]]*\}\s*)*\]', block, _re.DOTALL)
            has_enough_data = False
            for arr in array_literals:
                item_count = len(_re.findall(r'\{', arr))
                if item_count >= 3:
                    has_enough_data = True
                    break
            # 대안: 변수 참조로 데이터를 가져오는 경우도 OK
            if not has_enough_data:
                # fetch/API 호출이나 외부 데이터 소스 사용 여부
                has_fetch = bool(_re.search(r'(?:fetch|axios|useSWR|useQuery|getData|loadData)', block))
                has_enough_data = has_fetch

            c4_pass = has_enough_data
            checks.append({
                "name": f"mock_data:{filepath}",
                "pass": c4_pass,
            })
            if not c4_pass:
                structural_failures.append(
                    f"{filepath}: DataTable 사용 중이나 데이터 배열 3개 미만"
                )

        # ── Check 5: 'use client' 디렉티브 ──
        has_usestate = "useState" in block
        has_use_client = bool(_re.search(r"""^['"]use client['"]""", block, _re.MULTILINE))
        c5_pass = not has_usestate or has_use_client
        checks.append({
            "name": f"use_client_directive:{filepath}",
            "pass": c5_pass,
            "has_useState": has_usestate,
            "has_use_client": has_use_client,
        })
        if not c5_pass:
            structural_failures.append(
                f"{filepath}: useState 사용 중이나 'use client' 디렉티브 없음"
            )

    all_pass = len(structural_failures) == 0
    if all_pass:
        logger.info("harness_interactivity_pass checks=%d", len(checks))
    else:
        logger.warning(
            "harness_interactivity_fail failures=%d reasons=%s",
            len(structural_failures), [f[:80] for f in structural_failures[:3]],
        )
    return {
        "pass": all_pass,
        "structural_failures": structural_failures,
        "checks": checks,
    }


# ============================================================
# Harness QA — Design Match (디자인 HTML ↔ TSX 정적 비교)
# ============================================================

def _harness_validate_design_match(
    design_html: str,
    code: str,
) -> dict:
    """디자인 HTML과 생성된 TSX 코드의 구조적 일치도 정적 분석.

    검증 항목:
      1. 텍스트 콘텐츠 커버리지 — 디자인의 버튼/헤딩/테이블헤더 텍스트가 TSX에 존재
      2. 컴포넌트 커버리지 — 디자인의 table/form/card 등이 TSX에 React 컴포넌트로 존재
      3. 색상 토큰 사용 — 디자인의 hex 색상이 TSX에 반영
      4. 섹션 수 — 디자인과 TSX의 주요 섹션 수 대략 일치

    Args:
        design_html: 디자인 프리뷰 HTML 문자열
        code: 생성된 TSX 코드 (// FILE: 태그 포함 가능)

    Returns:
        {"pass": bool, "structural_failures": [...], "checks": [...]}
    """
    import re as _re
    from html.parser import HTMLParser

    checks: list[dict] = []
    structural_failures: list[str] = []

    if not design_html or not code:
        return {"pass": True, "structural_failures": [], "checks": [
            {"name": "design_match", "pass": True, "skipped": True, "reason": "no design or code"}
        ]}

    # ── 디자인 HTML 파싱 헬퍼 ──
    class _DesignExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.texts: list[str] = []
            self.tags: list[str] = []
            self.colors: set[str] = set()
            self.sections: int = 0
            self._current_tag: str = ""
            self._skip_tags = {"script", "style", "meta", "link"}
            self._in_skip = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._current_tag = tag
            self.tags.append(tag)
            if tag in self._skip_tags:
                self._in_skip = True
            if tag in ("section", "article", "main", "header", "footer", "nav"):
                self.sections += 1
            # 색상 추출
            for attr_name, attr_val in attrs:
                if attr_val and attr_name in ("style", "class"):
                    hex_colors = _re.findall(r'#[0-9a-fA-F]{3,8}', attr_val)
                    self.colors.update(c.lower() for c in hex_colors)
                if attr_val and attr_name == "style":
                    # rgb 색상도 추출
                    rgb_colors = _re.findall(r'rgb\([^)]+\)', attr_val)
                    self.colors.update(rgb_colors)

        def handle_endtag(self, tag: str) -> None:
            if tag in self._skip_tags:
                self._in_skip = False

        def handle_data(self, data: str) -> None:
            if self._in_skip:
                return
            text = data.strip()
            if text and len(text) >= 2:
                self.texts.append(text)

    extractor = _DesignExtractor()
    try:
        extractor.feed(design_html)
    except Exception:
        return {"pass": True, "structural_failures": [], "checks": [
            {"name": "design_match", "pass": True, "skipped": True, "reason": "HTML parse error"}
        ]}

    # ── 인라인 style 태그 내 색상도 추출 ──
    style_blocks = _re.findall(r'<style[^>]*>(.*?)</style>', design_html, _re.DOTALL | _re.IGNORECASE)
    for sb in style_blocks:
        hex_in_style = _re.findall(r'#[0-9a-fA-F]{3,8}', sb)
        extractor.colors.update(c.lower() for c in hex_in_style)

    # ── Check 1: 텍스트 콘텐츠 커버리지 ──
    # 버튼, 헤딩, th 텍스트를 중점 검사 (짧은 라벨/제목이 중요)
    important_texts: list[str] = []
    for t in extractor.texts:
        cleaned = t.strip()
        # 의미 있는 텍스트만 (너무 길거나 너무 짧은 건 스킵)
        if 2 <= len(cleaned) <= 50:
            important_texts.append(cleaned)

    # 중복 제거
    important_texts = list(dict.fromkeys(important_texts))

    missing_texts: list[str] = []
    for text in important_texts[:30]:  # 상위 30개만 검사
        # 정확 매칭 또는 코드 내 문자열 리터럴에 존재
        if text not in code:
            # 따옴표 안에 있는지도 확인
            escaped = _re.escape(text)
            if not _re.search(rf'["\'].*{escaped}.*["\']', code) and not _re.search(escaped, code):
                missing_texts.append(text)

    total_checked = min(len(important_texts), 30)
    coverage_ratio = (total_checked - len(missing_texts)) / total_checked if total_checked > 0 else 1.0
    c1_pass = coverage_ratio >= 0.6  # 60% 이상 텍스트 커버리지
    checks.append({
        "name": "text_coverage",
        "pass": c1_pass,
        "total_texts": total_checked,
        "missing_count": len(missing_texts),
        "coverage": round(coverage_ratio, 2),
        "missing_samples": missing_texts[:5],
    })
    if not c1_pass:
        structural_failures.append(
            f"디자인 텍스트 커버리지 {coverage_ratio:.0%} (기준 60%): "
            f"누락 예시 — {', '.join(missing_texts[:3])}"
        )

    # ── Check 2: 컴포넌트 커버리지 ──
    # 디자인 HTML 태그 → React 컴포넌트 매핑
    _TAG_TO_COMPONENT: dict[str, list[str]] = {
        "table": ["DataTable", "Table", "table", "<table"],
        "form": ["Form", "form", "<form", "onSubmit"],
        "canvas": ["Chart", "Graph", "canvas", "recharts", "chart"],
        "svg": ["Icon", "Chart", "svg"],
        "nav": ["Nav", "Navigation", "Sidebar", "TabBar", "nav"],
        "input": ["Input", "TextField", "SearchBar", "input"],
        "select": ["Select", "Dropdown", "select"],
        "textarea": ["TextArea", "textarea"],
    }

    design_has: list[str] = []
    missing_components: list[str] = []

    design_tag_set = set(extractor.tags)
    for html_tag, react_names in _TAG_TO_COMPONENT.items():
        if html_tag in design_tag_set:
            design_has.append(html_tag)
            found = any(name in code for name in react_names)
            if not found:
                missing_components.append(html_tag)

    c2_pass = len(missing_components) == 0 or len(missing_components) <= len(design_has) * 0.3
    checks.append({
        "name": "component_coverage",
        "pass": c2_pass,
        "design_elements": design_has,
        "missing_in_code": missing_components,
    })
    if not c2_pass:
        structural_failures.append(
            f"디자인 컴포넌트 미반영: {', '.join(missing_components)}"
        )

    # ── Check 3: 색상 토큰 사용 ──
    design_colors = extractor.colors
    if design_colors:
        code_lower = code.lower()
        matched_colors = 0
        for color in design_colors:
            color_l = color.lower()
            if color_l in code_lower:
                matched_colors += 1
            else:
                # 3자리 hex → 6자리 확장 비교
                if len(color_l) == 4:  # #rgb
                    expanded = f"#{color_l[1]*2}{color_l[2]*2}{color_l[3]*2}"
                    if expanded in code_lower:
                        matched_colors += 1
                # CSS 변수로 매핑되었을 수도 있으므로 관대하게 처리
                # var(--...) 사용 시 일부 색상 미매칭은 허용

        color_coverage = matched_colors / len(design_colors) if design_colors else 1.0
        # CSS 변수 사용 고려하여 30% 이상이면 통과
        c3_pass = color_coverage >= 0.3 or "var(--" in code
        checks.append({
            "name": "color_tokens",
            "pass": c3_pass,
            "design_colors": len(design_colors),
            "matched": matched_colors,
            "coverage": round(color_coverage, 2),
            "uses_css_vars": "var(--" in code,
        })
        if not c3_pass:
            structural_failures.append(
                f"색상 토큰 매칭 {color_coverage:.0%} (기준 30%), CSS 변수 미사용"
            )
    else:
        checks.append({"name": "color_tokens", "pass": True, "skipped": True, "reason": "no colors in design"})

    # ── Check 4: 섹션 수 비교 ──
    design_sections = max(extractor.sections, 1)
    # 디자인의 div[class*="section"] 카운트도 보조
    div_sections = len(_re.findall(
        r'<div[^>]*class="[^"]*(?:section|card|panel|block|container)[^"]*"',
        design_html, _re.IGNORECASE,
    ))
    design_sections = max(design_sections, div_sections)

    # TSX에서 섹션 수 추정
    code_sections = len(_re.findall(
        r'<(?:section|article|div\s+className="[^"]*(?:section|card|panel|block|container))',
        code, _re.IGNORECASE,
    ))
    # 최소 return 문 내의 주요 블록 수
    if code_sections == 0:
        code_sections = len(_re.findall(r'<div\s+className=', code))

    # 섹션 수가 디자인의 50~200% 범위 내이면 OK
    ratio = code_sections / design_sections if design_sections > 0 else 1.0
    c4_pass = 0.4 <= ratio <= 2.5 or design_sections <= 2
    checks.append({
        "name": "section_count",
        "pass": c4_pass,
        "design_sections": design_sections,
        "code_sections": code_sections,
        "ratio": round(ratio, 2),
    })
    if not c4_pass:
        structural_failures.append(
            f"섹션 수 불일치: 디자인 {design_sections}개 vs 코드 {code_sections}개 (비율 {ratio:.1f}x)"
        )

    all_pass = len(structural_failures) == 0
    if all_pass:
        logger.info("harness_design_match_pass checks=%d", len(checks))
    else:
        logger.warning(
            "harness_design_match_fail failures=%d reasons=%s",
            len(structural_failures), [f[:80] for f in structural_failures[:3]],
        )
    return {
        "pass": all_pass,
        "structural_failures": structural_failures,
        "checks": checks,
    }


# ============================================================
# Harness QA — 화면 커버리지 검증 (화면 목록 정의서 vs 디자인 시안/레시피)
# ============================================================

def _harness_validate_screen_coverage(
    screen_list_content: str,
    design_slugs: list[str],
    recipe_slugs: list[str],
    min_coverage: float = 0.7,
) -> dict:
    """화면 목록 정의서에 정의된 화면 수 vs 실제 디자인 시안/레시피 수 비교.

    DESIGN 단계의 QA에서 호출. 디자인 시안이나 레시피가 화면 목록 대비 크게 부족하면 FAIL.
    min_coverage: 호출자가 재시도 횟수에 따라 점진적으로 상향 가능 (0.6 → 0.75 → 0.90).

    Returns:
        {"pass": bool, "structural_failures": [...], "checks": [...]}
    """
    checks: list[dict] = []
    structural_failures: list[str] = []

    # ── 1. 화면 목록 정의서에서 화면 ID 추출 (다양한 포맷 수용) ──
    # 지원 포맷:
    #   - SCR-001 (레거시)
    #   - SC-AU-001, SC-CW-010 (프로젝트별 그룹 접두)
    #   - XX-001 ~ XXX-0001 (임의 2~4자 접두)
    # 테이블 행(| ID | 이름 |) 또는 일반 텍스트 모두에서 추출.
    defined_screens: list[dict] = []
    _seen_ids: set[str] = set()
    # 포괄 패턴: A-B-123, AB-001, SC-AU-001, SCR-123 등을 모두 매치
    generic_id_re = re.compile(
        r'\b(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})\b',
        re.IGNORECASE,
    )
    # 테이블 행에서 이름까지 추출 (| ID | 이름 | ... |)
    table_row_re = re.compile(
        r'\|\s*(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})\s*\|\s*([^|\n]+?)\s*\|',
        re.IGNORECASE,
    )
    for m in table_row_re.finditer(screen_list_content):
        scr_id = m.group(1).upper()
        if scr_id in _seen_ids:
            continue
        _seen_ids.add(scr_id)
        name = m.group(2).strip()
        defined_screens.append({"id": scr_id, "name": name})
    # 테이블 매치 못한 ID도 포함 (본문 서술형)
    for m in generic_id_re.finditer(screen_list_content):
        scr_id = m.group(1).upper()
        if scr_id in _seen_ids:
            continue
        _seen_ids.add(scr_id)
        # 이름 불명 — ID만으로라도 커버리지 카운트에 포함
        defined_screens.append({"id": scr_id, "name": ""})

    total_defined = len(defined_screens)
    if total_defined == 0:
        checks.append({"name": "screen_list_parsed", "pass": False, "total": 0})
        structural_failures.append(
            "화면 목록 정의서에서 화면을 추출할 수 없음 (SCR-XXX·SC-XX-XXX·XX-XXX 패턴 모두 0건)"
        )
        return {"pass": False, "structural_failures": structural_failures, "checks": checks}

    checks.append({"name": "screen_list_parsed", "pass": True, "total": total_defined})

    # ── 2. 디자인 시안 커버리지 (min_coverage 이상) ──
    # SCR 단위로 매칭: SCR 이름을 slug 형태로 변환해 design_slugs와 정규화 비교.
    # 한글도 보존하기 위해 Unicode word char(\w는 re.UNICODE로 한글 포함) 유지.
    def _normalize(s: str) -> str:
        # 구분자(공백, -, _, /, .)만 제거하고 영문·숫자·한글 그대로 유지
        return re.sub(r"[\s\-_/\.]+", "", s.lower())

    # design_slugs에서 ID 접두사 추출 — SCR-001, SC-AU-001, XX-001 모두 수용.
    # (e.g. "SCR-001-main-landing" → "SCR-001" / "SC-AU-001-login" → "SC-AU-001")
    # + normalize된 풀 slug도 함께 저장
    scr_id_re = re.compile(
        r"(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})",
        re.IGNORECASE,
    )
    design_norms: list[tuple[str | None, str]] = []
    for s in design_slugs:
        m = scr_id_re.search(s)
        scr_tag = m.group(1).upper() if m else None
        design_norms.append((scr_tag, _normalize(s)))

    # 파일명으로 매칭이 안 되는 SCR들을 명시적으로 수집
    missing_from_design: list[dict] = []
    matched_design: list[dict] = []
    for scr in defined_screens:
        scr_name_norm = _normalize(scr["name"])
        scr_id_norm = _normalize(scr["id"])
        # 1순위: SCR-ID 접두사 매치 (파일명에 "SCR-001" 포함)
        # 2순위: name/id 부분 문자열 매치 (레거시 호환)
        matched = False
        for tag, dn in design_norms:
            if tag == scr["id"]:
                matched = True
                break
            if scr_name_norm and (scr_name_norm in dn or dn in scr_name_norm):
                matched = True
                break
            if scr_id_norm in dn:
                matched = True
                break
        if matched:
            matched_design.append(scr)
        else:
            missing_from_design.append(scr)

    # 파일수 기반 단순 비율 (기존 동작 유지)
    design_count = len(design_slugs)
    design_ratio = design_count / total_defined if total_defined > 0 else 0
    # SCR 매칭 기반 엄격 비율 (신규)
    scr_match_ratio = len(matched_design) / total_defined if total_defined > 0 else 0
    # 실제 판정은 두 비율 중 낮은 쪽 (더 엄격) 사용
    effective_ratio = min(design_ratio, scr_match_ratio)
    c2_pass = effective_ratio >= min_coverage
    checks.append({
        "name": "design_coverage", "pass": c2_pass,
        "defined": total_defined, "designed": design_count,
        "ratio": round(design_ratio, 2),
        "scr_match_ratio": round(scr_match_ratio, 2),
        "effective_ratio": round(effective_ratio, 2),
        "threshold": round(min_coverage, 2),
        "missing_scrs": [f"{m['id']}|{m['name']}" for m in missing_from_design[:30]],
    })
    if not c2_pass:
        missing_summary = ", ".join(
            f"{m['id']}({m['name']})" for m in missing_from_design[:10]
        )
        structural_failures.append(
            f"디자인 시안 커버리지 부족: SCR 매칭 {len(matched_design)}/{total_defined} "
            f"({scr_match_ratio:.0%}). 최소 {min_coverage:.0%} 필요. "
            f"누락 SCR: {missing_summary}"
            + (f" 외 {len(missing_from_design)-10}건" if len(missing_from_design) > 10 else "")
        )

    # ── 3. 레시피 커버리지 (min_coverage 이상) ──
    recipe_count = len(recipe_slugs)
    recipe_ratio = recipe_count / total_defined if total_defined > 0 else 0
    c3_pass = recipe_ratio >= min_coverage
    checks.append({
        "name": "recipe_coverage", "pass": c3_pass,
        "defined": total_defined, "recipes": recipe_count,
        "ratio": round(recipe_ratio, 2),
        "threshold": round(min_coverage, 2),
    })
    if not c3_pass:
        structural_failures.append(
            f"페이지 레시피 커버리지 부족: {recipe_count}/{total_defined}개 ({recipe_ratio:.0%}). "
            f"최소 {min_coverage:.0%} 필요."
        )

    # ── 4. 관리자 페이지만 있고 사용자 화면 0인 경우 감지 ──
    admin_slugs = [s for s in recipe_slugs if "admin" in s.lower()]
    user_slugs = [s for s in recipe_slugs if "admin" not in s.lower()]

    if len(admin_slugs) > 0 and len(user_slugs) <= 1 and total_defined > 15:
        checks.append({
            "name": "user_facing_coverage", "pass": False,
            "admin_pages": len(admin_slugs), "user_pages": len(user_slugs),
        })
        structural_failures.append(
            f"사용자 대면 화면 심각하게 누락: 관리자 {len(admin_slugs)}개만 구현, "
            f"사용자 화면 {len(user_slugs)}개. 로그인/메인/사용자포털 등 필수."
        )
    else:
        checks.append({"name": "user_facing_coverage", "pass": True})

    all_pass = len(structural_failures) == 0
    if all_pass:
        logger.info("harness_screen_coverage_pass checks=%d", len(checks))
    else:
        logger.warning(
            "harness_screen_coverage_fail failures=%d reasons=%s",
            len(structural_failures), [f[:80] for f in structural_failures[:3]],
        )
    return {"pass": all_pass, "structural_failures": structural_failures, "checks": checks}


# ============================================================
# Harness QA — DEFINE 단계 교차참조 검증
# ============================================================

def _extract_scr_ids(text: str) -> set[str]:
    """문서 텍스트에서 화면 ID 참조를 모두 추출 (중복 제거).

    수용 포맷:
    - SCR-001 (레거시 단순)
    - SC-AU-001 (그룹 접두)
    - XX-001 ~ XXX-0001 (임의 2~4자 접두)
    """
    ids = re.findall(
        r"\b(SCR-\d{3,4}|SC-[A-Z]{2,4}-\d{3,4}|[A-Z]{2,4}-\d{3,4})\b",
        text, re.IGNORECASE,
    )
    return {m.upper() for m in ids}


def _harness_validate_define_cross_references(
    screen_list_content: str,
    user_flow_content: str = "",
    usecase_content: str = "",
    backlog_content: str = "",
    requirement_content: str = "",
) -> dict:
    """DEFINE 단계 산출물 간 교차 일관성 검증.

    검사 항목:
      1. 유저플로우/유스케이스에서 참조하는 모든 SCR이 화면목록에 존재.
      2. 화면목록의 모든 SCR이 유저플로우 또는 유스케이스에 최소 1회 등장.
      3. 기능 백로그의 핵심 기능이 요구사항 정의서에 대응 키워드로 존재.

    Returns: {"pass": bool, "structural_failures": [...], "checks": [...]}
    """
    checks: list[dict] = []
    failures: list[str] = []

    # 화면목록 SCR 집합
    scr_in_list = _extract_scr_ids(screen_list_content)
    if not scr_in_list:
        return {
            "pass": False,
            "structural_failures": [
                "화면목록 정의서에서 SCR-XXX 패턴을 찾을 수 없음"
            ],
            "checks": [],
        }

    # 유저플로우·유스케이스에 등장하는 SCR
    scr_in_flow = _extract_scr_ids(user_flow_content) | _extract_scr_ids(usecase_content)

    # 1) 유저플로우가 참조하는데 화면목록에 없는 것 (orphan reference)
    orphans = scr_in_flow - scr_in_list
    c1_pass = len(orphans) == 0
    checks.append({
        "name": "scr_orphan_references",
        "pass": c1_pass,
        "orphans": sorted(orphans)[:20],
    })
    if not c1_pass:
        failures.append(
            f"유저플로우/유스케이스가 참조하는 SCR {len(orphans)}개가 화면목록에 "
            f"정의되어 있지 않음: {sorted(orphans)[:10]}. "
            f"화면목록에 누락된 화면을 추가하거나 플로우의 참조를 수정해야 함."
        )

    # 2) 화면목록에는 있는데 플로우에 한 번도 등장하지 않는 것 (dangling screen)
    # user_flow_content + usecase_content 둘 다 비어있으면 스킵
    if user_flow_content or usecase_content:
        dangling = scr_in_list - scr_in_flow
        total = len(scr_in_list)
        dangling_ratio = len(dangling) / total if total else 0
        # 40% 초과 누락이면 심각 (문서 기반 소프트 임계값)
        c2_pass = dangling_ratio <= 0.4
        checks.append({
            "name": "scr_dangling_screens",
            "pass": c2_pass,
            "dangling": sorted(dangling)[:20],
            "ratio": round(dangling_ratio, 2),
        })
        if not c2_pass:
            failures.append(
                f"화면목록의 SCR {len(dangling)}/{total}개 ({dangling_ratio:.0%})가 "
                f"유저플로우/유스케이스에 한 번도 등장하지 않음. 플로우에 해당 화면 "
                f"이동 경로를 추가하거나 불필요한 화면을 화면목록에서 제거해야 함. "
                f"예시 누락: {sorted(dangling)[:5]}"
            )
    else:
        checks.append({"name": "scr_dangling_screens", "pass": True, "skipped": True})

    # 3) 백로그 ↔ 요구사항 키워드 매칭 (매우 느슨하게: 백로그 항목 제목의
    #    50% 이상이 요구사항 본문에 매치되는지)
    if backlog_content and requirement_content:
        # 백로그 항목 제목 추출 (마크다운 #, -, | 기반 heuristic)
        titles = re.findall(r"(?:^|\n)[-*#|]?\s*([가-힣\w][^\n|]{3,40})", backlog_content)
        # 너무 짧거나 공통어(공통, 기타 등) 제외
        keywords = {
            t.strip() for t in titles
            if len(t.strip()) >= 4 and t.strip() not in {"공통", "기타", "우선순위"}
        }
        if keywords:
            req_lower = requirement_content.lower()
            matched = sum(1 for k in keywords if k.lower() in req_lower)
            match_ratio = matched / len(keywords)
            c3_pass = match_ratio >= 0.5
            checks.append({
                "name": "backlog_requirement_match",
                "pass": c3_pass,
                "matched": matched, "total": len(keywords),
                "ratio": round(match_ratio, 2),
            })
            if not c3_pass:
                failures.append(
                    f"기능 백로그의 핵심 키워드 매치율 {matched}/{len(keywords)} "
                    f"({match_ratio:.0%}) — 요구사항 정의서와 정합성 부족. "
                    f"요구사항 문서에 백로그 기능들을 명시해야 함."
                )
        else:
            checks.append({"name": "backlog_requirement_match", "pass": True, "skipped": True})

    all_pass = len(failures) == 0
    if all_pass:
        logger.info("harness_define_xref_pass checks=%d", len(checks))
    else:
        logger.warning(
            "harness_define_xref_fail failures=%d reasons=%s",
            len(failures), [f[:80] for f in failures[:3]],
        )
    return {"pass": all_pass, "structural_failures": failures, "checks": checks}


# ---------------------------------------------------------------------------
# S10: JSON 산출물 전용 구조 검증 (type-aware QA)
# ---------------------------------------------------------------------------

def _harness_validate_json(
    content: str,
    spec: dict | None,
) -> dict:
    """JSON 산출물 구조 검증 — 토큰 0, 결정론적.

    document 의 _harness_validate_document 와 동일 인터페이스:
    Returns: {"pass": bool, "structural_failures": [...], "checks": [...]}

    검증 항목:
    1. json.loads() 파싱 성공
    2. 배열이면 len >= min_items (spec 정의 시)
    3. _incomplete 마킹 원소 비율 < 10%
    4. forbidden 키워드 체크
    5. required_keys 각 원소에 존재 (선택)
    """
    checks: list[dict] = []
    failures: list[str] = []

    if not content or not content.strip():
        failures.append("JSON 내용 없음 (빈 content)")
        return {"pass": False, "structural_failures": failures, "checks": checks}

    # 1. 파싱
    parsed = None
    try:
        parsed = json.loads(content)
        checks.append({"name": "json_parse", "pass": True})
    except Exception as e:
        failures.append(f"JSON 파싱 실패: {str(e)[:100]}")
        checks.append({"name": "json_parse", "pass": False, "error": str(e)[:80]})
        # 파싱 실패면 나머지 검증 무의미
        if failures:
            logger.warning("harness_json_fail parse_error=%s", str(e)[:80])
        return {"pass": False, "structural_failures": failures, "checks": checks}

    # 배열/객체 판별
    items = []
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = [parsed]

    # 2. min_items
    structural = (spec or {}).get("validation", {}).get("structural", {})
    min_items = structural.get("min_items", 0) or 0
    if min_items > 0:
        c_pass = len(items) >= min_items
        checks.append({"name": "min_items", "pass": c_pass,
                        "count": len(items), "min": min_items})
        if not c_pass:
            failures.append(f"JSON 원소 부족: {len(items)}개 < 최소 {min_items}개")

    # 3. _incomplete 비율
    incomplete = [i for i in items if isinstance(i, dict) and i.get("_incomplete")]
    if items:
        ratio = len(incomplete) / len(items)
        c_pass = ratio < 0.1
        checks.append({"name": "incomplete_ratio", "pass": c_pass,
                        "incomplete": len(incomplete), "total": len(items),
                        "ratio": round(ratio, 2)})
        if not c_pass:
            failures.append(
                f"미완성 원소 과다: {len(incomplete)}/{len(items)} "
                f"({ratio:.0%} >= 10%)"
            )

    # 4. forbidden 키워드 (문자열 값 전수 체크)
    forbidden_list = structural.get("forbidden", [])
    if forbidden_list:
        content_str = json.dumps(parsed, ensure_ascii=False)
        _forbidden_re = re.compile(
            r'(?<![a-zA-Z0-9_])(' + '|'.join(
                re.escape(f) for f in forbidden_list
            ) + r')(?![a-zA-Z0-9_])',
            re.IGNORECASE,
        )
        matches = list(_forbidden_re.finditer(content_str))
        c_pass = len(matches) == 0
        checks.append({"name": "forbidden", "pass": c_pass,
                        "count": len(matches)})
        if not c_pass:
            samples = [m.group(0) for m in matches[:3]]
            failures.append(f"금지어 {len(matches)}건: {', '.join(samples)}")

    all_pass = len(failures) == 0
    if all_pass:
        logger.info("harness_json_pass items=%d incomplete=%d",
                     len(items), len(incomplete))
    else:
        logger.warning("harness_json_fail failures=%d reasons=%s",
                        len(failures), [f[:60] for f in failures[:3]])
    return {"pass": all_pass, "structural_failures": failures, "checks": checks}


# ---------------------------------------------------------------------------
# S12: HTML 산출물 전용 구조 검증 (type-aware QA)
# ---------------------------------------------------------------------------

def _harness_validate_html(
    content: str,
    spec: dict | None,
) -> dict:
    """HTML 산출물 구조 검증 — 토큰 0, 결정론적.

    Returns: {"pass": bool, "structural_failures": [...], "checks": [...]}

    검증 항목:
    1. <!DOCTYPE html> 선언 존재
    2. <html> + <body> 태그 존재
    3. 헤딩 수 ≥ required_headings (spec 정의 시)
    4. <section> 수 ≥ min_items (chunk_items 쓴 경우)
    5. <style> 블록 또는 <link rel="stylesheet"> 존재
    6. 빈 섹션 비율 < 20% (본문 100자 이하 = 빈 판정)
    7. min_chars
    8. forbidden 키워드 (HTML 주석 내부 제외)
    """
    checks: list[dict] = []
    failures: list[str] = []

    if not content or not content.strip():
        failures.append("HTML 내용 없음")
        return {"pass": False, "structural_failures": failures, "checks": checks}

    content_lower = content.lower()

    # 1. DOCTYPE
    has_doctype = "<!doctype html" in content_lower
    checks.append({"name": "doctype", "pass": has_doctype})
    if not has_doctype:
        failures.append("<!DOCTYPE html> 선언 누락")

    # 2. html/body 태그
    has_html = "<html" in content_lower
    has_body = "<body" in content_lower
    checks.append({"name": "html_body", "pass": has_html and has_body})
    if not (has_html and has_body):
        failures.append("<html> 또는 <body> 태그 누락")

    # 3. 헤딩 수
    heading_matches = re.findall(r"<h[1-6][^>]*>.*?</h[1-6]>", content, re.IGNORECASE | re.DOTALL)
    heading_count = len(heading_matches)
    checks.append({"name": "heading_count", "pass": heading_count > 0, "count": heading_count})

    structural = (spec or {}).get("validation", {}).get("structural", {})
    required_headings = structural.get("required_headings") or []
    if required_headings:
        min_h = len(required_headings)
        if heading_count < min_h:
            failures.append(f"헤딩 수 부족: {heading_count} < {min_h}")

    # 4. <section> 수
    section_matches = re.findall(r"<section[^>]*>", content, re.IGNORECASE)
    section_count = len(section_matches)
    checks.append({"name": "section_count", "pass": True, "count": section_count})
    min_items = structural.get("min_items", 0) or 0
    if min_items > 0:
        if section_count < min_items:
            failures.append(f"<section> 수 부족: {section_count} < {min_items}")

    # 5. style 존재
    has_style = ("<style" in content_lower
                 or "<link" in content_lower and "stylesheet" in content_lower
                 or 'style="' in content_lower)
    checks.append({"name": "has_style", "pass": has_style})
    if not has_style:
        failures.append("<style> 또는 외부 CSS <link> 없음")

    # 6. 빈 섹션 비율 (section 태그 쓴 경우만)
    # 본문 50자 미만 = 사실상 shell 로 간주 (heading·form 만 있고 실제 설명 없음)
    if section_count >= 3:
        section_bodies = re.findall(
            r"<section[^>]*>([\s\S]*?)</section>", content, re.IGNORECASE,
        )
        empty_sections = sum(
            1 for body in section_bodies
            if len(re.sub(r"<[^>]+>", "", body).strip()) < 50
        )
        empty_ratio = empty_sections / len(section_bodies) if section_bodies else 0
        checks.append({
            "name": "empty_section_ratio", "pass": empty_ratio < 0.3,
            "empty": empty_sections, "total": len(section_bodies),
        })
        if empty_ratio >= 0.3:
            failures.append(
                f"빈 section 과다: {empty_sections}/{len(section_bodies)} ({empty_ratio:.0%})"
            )

    # 7. min_chars
    min_chars = structural.get("min_chars", 0) or 0
    if min_chars > 0:
        c_pass = len(content) >= min_chars
        checks.append({"name": "min_chars", "pass": c_pass, "size": len(content), "min": min_chars})
        if not c_pass:
            failures.append(f"분량 부족: {len(content)} < {min_chars}자")

    # 7-R. 풀 반응형 safeguard CSS 포함 여부 (엔진 전역 규칙)
    # engine/skills/specs/_common/responsive_rules.md 의 필수 CSS 패턴 체크.
    # 구체적으로 다음 핵심 2가지 존재하면 PASS:
    #   (a) `min-width: 0` 을 * 또는 전역 선택자에 적용
    #   (b) `overflow-wrap: anywhere` 또는 `word-break: keep-all` 적용
    # 없으면 카드/그리드가 모바일에서 깨지므로 경고 (FAIL 아닌 warn).
    _has_min_width_zero = bool(re.search(
        r'\*\s*[^{]*\{[^}]*min-width\s*:\s*0',
        content, re.IGNORECASE,
    ))
    _has_overflow_wrap = bool(re.search(
        r'overflow-wrap\s*:\s*(?:anywhere|break-word)',
        content, re.IGNORECASE,
    )) or bool(re.search(
        r'word-break\s*:\s*(?:keep-all|break-all|break-word)',
        content, re.IGNORECASE,
    ))
    _responsive_ok = _has_min_width_zero and _has_overflow_wrap
    checks.append({
        "name": "responsive_safeguard",
        "pass": _responsive_ok,
        "min_width_zero": _has_min_width_zero,
        "overflow_wrap": _has_overflow_wrap,
    })
    if not _responsive_ok:
        failures.append(
            "풀 반응형 safeguard CSS 누락 — "
            "* { min-width: 0 } + overflow-wrap/word-break 필수 "
            "(_common/responsive_rules.md 참조)"
        )

    # 8. forbidden 키워드 (HTML 주석 영역 제외)
    forbidden_list = structural.get("forbidden", [])
    if forbidden_list:
        # HTML 주석 제거 후 검사
        stripped = re.sub(r"<!--[\s\S]*?-->", "", content)
        forbidden_re = re.compile(
            r'(?<![a-zA-Z0-9_])(' + '|'.join(
                re.escape(f) for f in forbidden_list
            ) + r')(?![a-zA-Z0-9_])',
            re.IGNORECASE,
        )
        matches = list(forbidden_re.finditer(stripped))
        c_pass = len(matches) == 0
        checks.append({"name": "forbidden", "pass": c_pass, "count": len(matches)})
        if not c_pass:
            samples = [m.group(0) for m in matches[:3]]
            failures.append(f"금지어 {len(matches)}건: {', '.join(samples)}")

    all_pass = len(failures) == 0
    if all_pass:
        logger.info("harness_html_pass sections=%d headings=%d size=%d",
                     section_count, heading_count, len(content))
    else:
        logger.warning("harness_html_fail failures=%d reasons=%s",
                        len(failures), [f[:60] for f in failures[:3]])
    return {"pass": all_pass, "structural_failures": failures, "checks": checks}


# ---------------------------------------------------------------------------
# Partial Cascade 무결성 검증
# ---------------------------------------------------------------------------

def _extract_headings(text: str) -> list[str]:
    """마크다운 텍스트에서 ## / ### 헤딩 추출."""
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            headings.append(stripped)
    return headings


def _harness_validate_partial_patch(
    old_artifact: str,
    new_artifact: str,
    affected_sections: list[str],
) -> dict:
    """
    PARTIAL 패치 완료 직후 프로그래매틱 무결성 검증.
    AI QA 전 단계에서 drift를 코드로 조기 감지.

    검증 항목:
      C1: affected_sections 헤딩이 new_artifact에 모두 존재
      C2: old_artifact의 비수정 헤딩이 new_artifact에도 보존
      C3: 문서 길이가 원문 대비 합리적 범위 (50%~200%)
      C4: new_artifact가 실질 내용 포함 (200자 이상)

    비수정 섹션은 헤딩 존재 여부만 확인 (텍스트 완전 일치 아님 → 오탐 방지).

    Returns:
        {
          "pass": bool,
          "checks": list[dict],
          "failures": list[str],
          "trigger_full_rerun": bool,   # True → PARTIAL 포기, 전체 재실행 필요
        }
    """
    checks: list[dict] = []
    failures: list[str] = []

    old_headings = _extract_headings(old_artifact)
    new_headings = _extract_headings(new_artifact)
    new_headings_set = set(new_headings)

    # ── C1: affected_sections 헤딩이 new_artifact에 존재 ──
    missing_affected = [s for s in affected_sections if s not in new_headings_set]
    c1_pass = len(missing_affected) == 0
    checks.append({
        "name": "affected_sections_present",
        "pass": c1_pass,
        "expected": affected_sections,
        "missing": missing_affected,
    })
    if not c1_pass:
        failures.append(
            f"패치 대상 섹션 누락: {missing_affected} — AI가 섹션 헤딩을 삭제했거나 명칭 변경됨"
        )

    # ── C2: 비수정 헤딩 보존 여부 ──
    # old 헤딩 중 affected_sections가 아닌 것들이 new에도 있어야 함
    unmodified_headings = [h for h in old_headings if h not in affected_sections]
    missing_unmodified = [h for h in unmodified_headings if h not in new_headings_set]
    # 비수정 섹션 중 10% 초과 누락 시 FAIL (소수 리팩터링 허용)
    threshold_count = max(1, int(len(unmodified_headings) * 0.10))
    c2_pass = len(missing_unmodified) <= threshold_count
    checks.append({
        "name": "unmodified_sections_preserved",
        "pass": c2_pass,
        "total_unmodified": len(unmodified_headings),
        "missing_count": len(missing_unmodified),
        "threshold": threshold_count,
        "missing": missing_unmodified[:5],  # 로그용 최대 5개
    })
    if not c2_pass:
        failures.append(
            f"비수정 섹션 {len(missing_unmodified)}개 손실 (허용 {threshold_count}개): "
            f"{missing_unmodified[:3]}"
        )

    # ── C3: 문서 길이 범위 (50%~200%) ──
    old_len = len(old_artifact)
    new_len = len(new_artifact)
    if old_len > 0:
        ratio = new_len / old_len
        c3_pass = 0.50 <= ratio <= 2.00
        checks.append({
            "name": "length_range",
            "pass": c3_pass,
            "old_len": old_len,
            "new_len": new_len,
            "ratio": round(ratio, 2),
        })
        if not c3_pass:
            if ratio < 0.50:
                failures.append(
                    f"문서 길이 급감: {old_len}자 → {new_len}자 ({ratio:.0%}) — 내용 대규모 삭제 의심"
                )
            else:
                failures.append(
                    f"문서 길이 과다 증가: {old_len}자 → {new_len}자 ({ratio:.0%}) — 내용 폭증 의심"
                )
    else:
        checks.append({"name": "length_range", "pass": True, "note": "old_artifact 비어있어 생략"})

    # ── C4: 실질 내용 존재 ──
    c4_pass = len(new_artifact.strip()) >= 200
    checks.append({
        "name": "minimum_content",
        "pass": c4_pass,
        "length": new_len,
        "minimum": 200,
    })
    if not c4_pass:
        failures.append(f"패치 결과 내용 부족: {new_len}자 (최소 200자 필요)")

    all_pass = len(failures) == 0
    return {
        "pass": all_pass,
        "checks": checks,
        "failures": failures,
        "trigger_full_rerun": not all_pass,
    }


# ---------------------------------------------------------------------------
# Stage 10: Formal Schema Validation (JSON Schema strict)
# ---------------------------------------------------------------------------

def _harness_validate_schema(
    content: str,
    spec: dict,
    schemas_dir: str | None = None,
) -> dict:
    """spec.output_schema 선언 기반 JSON Schema strict 검증.

    spec yaml 필드:
        output_schema:
          type: json                       # json | html (html 은 구조만)
          schema_ref: schemas/ia.json      # 파일 경로 (engine/skills/schemas/)
          strict: true                     # strict 시 추가 필드 금지
          on_fail: retry | warn | fail    # 실패 처리 (상위에서 분기)

    반환:
        {
          "pass": bool,
          "checks": [{"name": ..., "pass": ...}],
          "failures": [...],
          "on_fail": "retry"|"warn"|"fail",
          "schema_applied": bool,
        }

    jsonschema 미설치 시 check 스킵 (soft fail=pass).
    """
    checks: list[dict] = []
    failures: list[str] = []

    schema_cfg = spec.get("output_schema") if isinstance(spec, dict) else None
    if not schema_cfg or not isinstance(schema_cfg, dict):
        # output_schema 미선언 → 검증 스킵
        return {
            "pass": True, "checks": [], "failures": [],
            "on_fail": "warn", "schema_applied": False,
        }

    on_fail = schema_cfg.get("on_fail", "warn")
    schema_ref = schema_cfg.get("schema_ref")
    strict = bool(schema_cfg.get("strict", False))
    schema_type = schema_cfg.get("type", "json")

    # 1) 스키마 파일 로드
    schema_obj: dict | None = None
    if schema_ref:
        try:
            from pathlib import Path
            base = Path(schemas_dir) if schemas_dir else Path(__file__).parent.parent / "schemas"
            schema_path = base / schema_ref.replace("schemas/", "", 1)
            if not schema_path.exists():
                # schemas_dir 포함 절대 경로 시도
                alt = Path(schema_ref)
                if alt.exists():
                    schema_path = alt
            if schema_path.exists():
                schema_obj = json.loads(schema_path.read_text(encoding="utf-8"))
            else:
                failures.append(f"schema 파일 없음: {schema_ref}")
                checks.append({"name": "schema_file_exists", "pass": False})
        except Exception as e:
            failures.append(f"schema 로드 실패: {str(e)[:150]}")
            checks.append({"name": "schema_load", "pass": False})
            schema_obj = None

    if schema_obj is None:
        return {
            "pass": len(failures) == 0,
            "checks": checks,
            "failures": failures,
            "on_fail": on_fail,
            "schema_applied": False,
        }

    # 2) content 파싱 (json 전용)
    if schema_type == "json":
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except Exception as e:
            failures.append(f"JSON 파싱 실패: {str(e)[:150]}")
            return {
                "pass": False, "checks": checks, "failures": failures,
                "on_fail": on_fail, "schema_applied": True,
            }
    else:
        # html 등 비 json 타입은 present 검증만 패스
        return {
            "pass": True, "checks": [{"name": "schema_type", "pass": True, "note": "non-json skip"}],
            "failures": [], "on_fail": on_fail, "schema_applied": False,
        }

    # 3) jsonschema 로 검증 (라이브러리 미설치 시 graceful)
    try:
        import jsonschema
    except ImportError:
        logger.warning("jsonschema 미설치 — schema validation 스킵")
        return {
            "pass": True, "checks": [],
            "failures": ["jsonschema 라이브러리 미설치"],
            "on_fail": "warn", "schema_applied": False,
        }

    # strict 시 additionalProperties=False 강제
    if strict and isinstance(schema_obj, dict):
        if schema_obj.get("type") == "object" and "additionalProperties" not in schema_obj:
            schema_obj = {**schema_obj, "additionalProperties": False}

    validator_cls = jsonschema.Draft202012Validator
    try:
        validator = validator_cls(schema_obj)
        errors = list(validator.iter_errors(payload))
        if errors:
            for err in errors[:10]:  # 처음 10개만 표시
                path = ".".join(str(p) for p in err.absolute_path)
                failures.append(f"{path or '<root>'}: {err.message[:150]}")
            checks.append({
                "name": "jsonschema", "pass": False,
                "error_count": len(errors),
            })
        else:
            checks.append({"name": "jsonschema", "pass": True})
    except jsonschema.SchemaError as e:
        failures.append(f"스키마 자체 오류: {str(e)[:150]}")
        checks.append({"name": "schema_valid", "pass": False})
    except Exception as e:
        failures.append(f"검증 예외: {str(e)[:150]}")
        checks.append({"name": "validation_exception", "pass": False})

    return {
        "pass": len(failures) == 0,
        "checks": checks,
        "failures": failures,
        "on_fail": on_fail,
        "schema_applied": True,
    }
