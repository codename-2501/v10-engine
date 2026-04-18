"""
engine/workspace/programmatic_verify.py
프로그래매틱 코드 빌드 검증 (AI 0회, 토큰 0).

workspace 기록 후 next build → 에러 분석 → 패턴 기반 자동 수정 → 재시도.
"""

from __future__ import annotations

import logging
import re
import subprocess
import asyncio
from pathlib import Path

from engine.workspace.paths import _sanitize_code_for_workspace

logger = logging.getLogger("engine.workspace.programmatic_verify")

MAX_PROGRAMMATIC_FIX_ATTEMPTS = 3


async def programmatic_build_verify(
    workspace_path: Path,
    component_path_map: dict[str, str] | None = None,
) -> dict:
    """프로그래매틱 코드 workspace 쓰기 후 빌드 검증 + 자동 수정 루프.

    next build → 에러 분석 → 패턴 기반 자동 수정 → 재시도. max 3회, AI 0회.

    수정 패턴:
      - Module not found → workspace 스캔해서 실제 경로로 치환
      - 마크다운/Python 잔여물 → _sanitize_code_for_workspace 재적용
      - TS 타입 에러 → 기존 _auto_fix_ts_errors 재사용
      - Missing export → default export 추가
    """
    fe = workspace_path / "frontend"
    if not fe.is_dir():
        return {"success": True, "skipped": True, "reason": "no frontend dir"}

    # package.json / node_modules 확인
    if not (fe / "package.json").is_file():
        return {"success": True, "skipped": True, "reason": "no package.json"}
    if not (fe / "node_modules").is_dir():
        return {"success": True, "skipped": True, "reason": "no node_modules"}

    # workspace 컴포넌트 맵 구축 (전달받지 못했으면 직접 스캔)
    cpm = component_path_map or _scan_component_paths(fe)

    all_fixes: list[str] = []

    # 빌드 전에 라우트 구조 선검증 — Next.js build 자체가 "same slug" 에러를
    # 모호하게 리포트해서 패턴 매칭이 어려운 경우가 있어, 먼저 SSOT 검증기로
    # 중복 슬러그를 찾고 자동 rename 적용.
    try:
        pre_fixes = _auto_fix_route_slug_collisions(fe)
        if pre_fixes:
            all_fixes.extend(pre_fixes)
            logger.info("programmatic_prebuild_route_fixes count=%d", len(pre_fixes))
    except Exception as exc:
        logger.warning("programmatic_prebuild_route_check_failed error=%s", exc)

    for attempt in range(1, MAX_PROGRAMMATIC_FIX_ATTEMPTS + 1):
        result = await asyncio.to_thread(subprocess.run, 
            ["npx", "next", "build"],
            cwd=fe, capture_output=True, text=True, timeout=180,
        )

        if result.returncode == 0:
            logger.info("programmatic_build_ok attempt=%d fixes=%d", attempt, len(all_fixes))
            return {"success": True, "attempts": attempt, "fixes": all_fixes}

        stderr = result.stdout + result.stderr

        # Next.js가 stderr로 "same slug name" 에러를 냈을 때 자동 rename 후 재빌드
        if (
            "same slug name" in stderr
            or "repeat within a single dynamic path" in stderr
        ):
            slug_fixes = _auto_fix_route_slug_collisions(fe)
            if slug_fixes:
                all_fixes.extend(slug_fixes)
                logger.info(
                    "programmatic_build_slug_fix attempt=%d fixes=%d",
                    attempt, len(slug_fixes),
                )
                continue  # 재빌드

        fixes = _fix_programmatic_errors(fe, stderr, cpm)

        if not fixes:
            # 기존 nextjs 패턴도 시도
            from engine.workspace.verify_and_fix import _auto_fix_nextjs_errors
            fixes = _auto_fix_nextjs_errors(fe, stderr)

        if not fixes:
            # TS 에러 패턴 시도
            from engine.workspace.verify_and_fix import _parse_ts_errors, _auto_fix_ts_errors
            ts_errors = _parse_ts_errors(stderr)
            if ts_errors:
                fixes = _auto_fix_ts_errors(fe, ts_errors)

        if not fixes:
            logger.warning(
                "programmatic_build_unfixable attempt=%d errors=%s",
                attempt, stderr[-300:],
            )
            return {
                "success": False,
                "attempts": attempt,
                "fixes": all_fixes,
                "errors": [stderr[-500:]],
            }

        all_fixes.extend(fixes)
        logger.info("programmatic_build_fixed attempt=%d fixes=%d", attempt, len(fixes))

    return {
        "success": False,
        "attempts": MAX_PROGRAMMATIC_FIX_ATTEMPTS,
        "fixes": all_fixes,
        "errors": ["max attempts reached"],
    }


def _auto_fix_route_slug_collisions(fe: Path) -> list[str]:
    """Next.js 라우트 구조 문제 자동 해결.

    처리 항목 (순서 중요):
    1. 유효하지 않은 폴더명 (template leak `{data`, placeholder `grp-01`) 정리
    2. 동일 리소스 중복 경로 (admin-x vs admin/x) 정리 — 더 깊고 깨끗한 경로 유지
    3. 동일 경로 내 [slug] 중복 rename ([id]/[id] → [id]/[careLogId])

    자동수정 이후 재검증하여 잔존 문제는 errors로 남김.
    """
    from engine.workspace.nextjs_route_validator import (
        _locate_app_dir,
        apply_duplicate_resource_cleanup,
        apply_invalid_folder_cleanup,
        apply_rename_suggestions,
        validate_nextjs_routes,
    )

    app_dir = _locate_app_dir(fe)
    if app_dir is None:
        return []

    result = validate_nextjs_routes(fe)
    if result.ok:
        return []

    fixes: list[str] = []

    # 1) 잘못된 폴더명 정리 (우선순위 최고 — 다른 검사의 노이즈 원인)
    if result.invalid_folders:
        actions = apply_invalid_folder_cleanup(result.invalid_folders, app_dir)
        for path, action in actions:
            fixes.append(
                f"invalid_folder[{action}]: {path.relative_to(app_dir)}"
            )

    # 2) 중복 리소스 정리 (1 이후 재검증 필요)
    result2 = validate_nextjs_routes(fe)
    if result2.duplicate_resources:
        actions = apply_duplicate_resource_cleanup(
            result2.duplicate_resources, app_dir,
        )
        for path, action in actions:
            fixes.append(
                f"duplicate_resource[{action}]: {path.relative_to(app_dir)}"
            )

    # 3) 중복 slug rename (1,2 이후 최종 정리)
    result3 = validate_nextjs_routes(fe)
    if result3.rename_suggestions:
        applied = apply_rename_suggestions(result3.rename_suggestions, app_dir)
        for old, new, refs in applied:
            fixes.append(
                f"route_slug_rename: {old.relative_to(fe)} -> "
                f"{new.relative_to(fe)} (params_refs={refs})"
            )

    return fixes


def _fix_programmatic_errors(
    fe: Path,
    stderr: str,
    cpm: dict[str, str],
) -> list[str]:
    """프로그래매틱 코드 특화 에러 수정 (AI 0회).

    패턴:
      1. Module not found: Can't resolve '@/components/...' → 실제 경로로 치환
      2. Module not found: Can't resolve './...' → 상대 경로 스캔 매칭
      3. 마크다운 잔여물 (```, ##, ---) → _sanitize 재적용
      4. Unexpected token — Python 문법 잔재 → 정리
      5. export default 누락 → 추가
    """
    fixes = []

    # ── 1. Module not found: @/components/... ──
    for m in re.finditer(
        r"Module not found.*?Can't resolve ['\"](@/components/\S+?)['\"].*?in ['\"](\S+?)['\"]",
        stderr,
    ):
        bad_path = m.group(1)  # @/components/ui/Button
        source_dir = m.group(2)  # /abs/path/to/src/pages

        # bad_path에서 컴포넌트명 추출
        comp_name = bad_path.rsplit("/", 1)[-1]
        if comp_name in cpm:
            correct_path = cpm[comp_name]
            # bad_path를 사용하는 모든 .tsx 파일에서 치환
            fixed = _replace_import_in_workspace(fe, bad_path, correct_path)
            if fixed:
                fixes.extend(fixed)

    # 패턴 변형: Module not found: Can't resolve '@/components'  (배럴 import)
    if "Can't resolve '@/components'" in stderr or 'Can\'t resolve "@/components"' in stderr:
        fixed = _split_barrel_imports(fe, cpm)
        if fixed:
            fixes.extend(fixed)

    # ── 2. Module not found: 상대 경로 (./) ──
    for m in re.finditer(
        r"Module not found.*?Can't resolve ['\"](\./\S+?)['\"].*?in ['\"](\S+?)['\"]",
        stderr,
    ):
        rel_path = m.group(1)  # ./components/Button
        source_file_dir = m.group(2)

        comp_name = rel_path.rsplit("/", 1)[-1]
        if comp_name in cpm:
            correct_path = cpm[comp_name]
            fixed = _replace_import_in_workspace(fe, rel_path, correct_path)
            if fixed:
                fixes.extend(fixed)

    # ── 3. 마크다운 잔여물 / Python 문법 ──
    # 파일 경로가 에러에 포함된 경우 해당 파일 재정리
    syntax_files = set()
    for m in re.finditer(r"(src/\S+\.tsx?)\((\d+),(\d+)\)", stderr):
        syntax_files.add(m.group(1))
    # "Unexpected token" 또는 "Expression expected" 에러가 있는 파일
    for m in re.finditer(r"([\w/.\-]+\.tsx?)\(\d+,\d+\).*(?:Unexpected token|Expression expected|Unterminated)", stderr):
        syntax_files.add(m.group(1))

    for rel_path in syntax_files:
        abs_path = fe / rel_path
        if not abs_path.is_file():
            abs_path = fe / "src" / rel_path
        if not abs_path.is_file():
            continue

        try:
            content = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # 마크다운 잔여물 징후 감지
        has_markdown = (
            "```" in content
            or re.search(r'^#{1,4}\s+\w', content, re.MULTILINE)
            or "\n---\n" in content
        )
        # Python 잔재 감지
        has_python = (
            "{True}" in content
            or "{False}" in content
            or "{None}" in content
        )

        if has_markdown or has_python:
            cleaned = _sanitize_code_for_workspace(content, rel_path, component_path_map=cpm)
            if cleaned != content:
                abs_path.write_text(cleaned, encoding="utf-8")
                fixes.append(f"re-sanitize: {rel_path}")

    # ── 4. export default 누락 ──
    for m in re.finditer(r"(src/\S+\.tsx)\b.*does not have a default export", stderr):
        rel_path = m.group(1)
        abs_path = fe / rel_path
        if not abs_path.is_file():
            continue
        content = abs_path.read_text(encoding="utf-8")
        if "export default" not in content:
            # 첫 번째 function 또는 const 컴포넌트에 export default 추가
            content = re.sub(
                r'^(function\s+(\w+))',
                r'export default function \2',
                content,
                count=1,
                flags=re.MULTILINE,
            )
            if "export default" not in content:
                content = re.sub(
                    r'^(const\s+(\w+)\s*=)',
                    r'const \2 =',
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                # 파일 끝에 export default 추가
                func_match = re.search(r'(?:function|const)\s+(\w+)', content)
                if func_match:
                    content += f"\n\nexport default {func_match.group(1)};\n"
            abs_path.write_text(content, encoding="utf-8")
            fixes.append(f"add export default: {rel_path}")

    return fixes


def _replace_import_in_workspace(
    fe: Path,
    bad_path: str,
    correct_path: str,
) -> list[str]:
    """workspace 전체에서 잘못된 import 경로를 올바른 경로로 치환."""
    fixes = []
    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if bad_path not in content:
            continue

        new_content = content.replace(f"'{bad_path}'", f"'{correct_path}'")
        new_content = new_content.replace(f'"{bad_path}"', f'"{correct_path}"')
        if new_content != content:
            tsx_file.write_text(new_content, encoding="utf-8")
            rel = tsx_file.relative_to(fe)
            fixes.append(f"import fix: {rel} ({bad_path} → {correct_path})")
    return fixes


def _split_barrel_imports(fe: Path, cpm: dict[str, str]) -> list[str]:
    """'@/components' 배럴 import를 개별 경로로 분리."""
    fixes = []
    for tsx_file in fe.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # import { A, B, C } from '@/components';
        m = re.search(
            r"import\s+\{\s*(.+?)\s*\}\s+from\s+['\"]@/components['\"];?",
            content,
        )
        if not m:
            continue

        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        new_imports = []
        for name in names:
            if name in cpm:
                new_imports.append(f"import {name} from '{cpm[name]}';")
            else:
                new_imports.append(f"import {name} from '@/components/{name}';")

        new_content = content.replace(m.group(0), "\n".join(new_imports))
        if new_content != content:
            tsx_file.write_text(new_content, encoding="utf-8")
            rel = tsx_file.relative_to(fe)
            fixes.append(f"barrel split: {rel} ({len(names)} components)")

    return fixes


def _scan_component_paths(fe: Path) -> dict[str, str]:
    """frontend/src/ 디렉토리를 스캔하여 PascalName → @/ import 경로 맵."""
    result: dict[str, str] = {}
    src = fe / "src"
    if not src.is_dir():
        return result
    for tsx_file in src.rglob("*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        name = tsx_file.stem
        if name.startswith("_") or name == "index" or not name[0].isupper():
            continue
        rel = tsx_file.relative_to(src)
        result[name] = "@/" + str(rel.with_suffix("")).replace("\\", "/")
    return result
