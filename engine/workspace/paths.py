"""
engine/workspace/paths.py
Shared workspace utility functions: slug generation, code sanitization,
path resolution, and related constants.

Extracted from auto_deploy.py for reuse across workspace modules.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

WORKSPACES_ROOT = Path(__file__).resolve().parent.parent.parent / "workspaces"

_NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "dns", "domain", "events", "fs", "http", "http2",
    "https", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "sys", "timers", "tls", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
})

_SKIP_PACKAGES = frozenset({
    "msw", "@playwright/test", "playwright", "jest", "@jest/globals",
    "vitest", "cypress", "puppeteer", "supertest", "@testing-library/react",
    "@testing-library/jest-dom", "styled-jsx",
})


def _is_node_builtin_or_skip(pkg: str) -> bool:
    """Node 내장 모듈이나 npm 의존성으로 넣으면 안 되는 패키지 판별."""
    if pkg.startswith("node:"):
        return True
    if pkg in _NODE_BUILTINS:
        return True
    if pkg in _SKIP_PACKAGES:
        return True
    return False


def _npm_safe_name(raw: str) -> str:
    """디렉토리명 → npm package name (영문+숫자+하이픈만 허용).

    한글 제거, 공백→하이픈, 연속하이픈 정리, 소문자 강제.
    EINVALIDPACKAGENAME 방지.
    """
    # 영문/숫자/하이픈만 남기기
    safe = re.sub(r'[^a-zA-Z0-9\-]', '-', raw)
    safe = re.sub(r'-{2,}', '-', safe).strip('-').lower()
    return safe or "app"


def _make_slug(name: str) -> str:
    """engagement name → workspace 디렉토리명.

    규칙:
      1. " — " 또는 " - " 이전 부분만 사용 (설명 텍스트 제거)
      2. 한글만 있으면 그대로 사용 (디렉토리명으로 유효)
      3. 공백 → 하이픈
      4. 연속 하이픈 정리
    """
    # "명성실버케어센터 디지털 전환 플랫폼 — 웹서비스" → "명성실버케어센터 디지털 전환 플랫폼"
    short = name.split(" — ")[0].split(" - ")[0].strip()
    if not short:
        short = name
    # 공백 → 하이픈, 연속 하이픈 정리
    slug = re.sub(r'\s+', '-', short).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    return slug


def _sanitize_code_for_workspace(
    code: str,
    filepath: str,
    component_path_map: dict[str, str] | None = None,
) -> str:
    """코드 블록을 workspace 파일로 쓰기 전 자동 정리.

    범용 처리 — AI/프로그래매틱 양쪽 코드에 적용:
      1. 마크다운 코드펜스 (```) 제거
      2. 마크다운 헤딩 (## , ### ) 제거
      3. 마크다운 구분선 (---) 제거
      4. Python 문법 → JS 변환 (True→true, False→false, None→null)
      5. import 경로 자동 보정 (component_path_map 있으면 잘못된 경로 수정)
    """
    lines = code.split("\n")
    cleaned: list[str] = []
    in_fence = False

    for line in lines:
        stripped = line.strip()

        # (1) 마크다운 코드펜스 제거
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        # (2) 마크다운 헤딩 제거 (코드 파일 안에 ## 섹션이 섞인 경우)
        if stripped.startswith("#") and not stripped.startswith("#!") and not stripped.startswith("#include"):
            if re.match(r'^#{1,4}\s+[^\{]', stripped):
                continue

        # (3) 마크다운 구분선 제거 (정확히 --- 또는 *** 만)
        if stripped in ("---", "***", "___"):
            continue

        # (3b) HTML 주석 제거 (FILE_MANIFEST, SELF_CHECK 등 — .tsx 파일에 섞이면 빌드 에러)
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue

        cleaned.append(line)

    result = "\n".join(cleaned)

    # (4) Python 리터럴 → JS 변환 (JSX/TS 파일만)
    if filepath.endswith((".tsx", ".jsx", ".ts", ".js")):
        result = re.sub(r'\{True\}', '{true}', result)
        result = re.sub(r'\{False\}', '{false}', result)
        result = re.sub(r'\{None\}', '{null}', result)
        result = re.sub(r'(?<=[=!<>& |,(\[:])\s*True\b', ' true', result)
        result = re.sub(r'(?<=[=!<>& |,(\[:])\s*False\b', ' false', result)
        result = re.sub(r'(?<=[=!<>& |,(\[:])\s*None\b', ' null', result)
        result = re.sub(r'(\w+)\.\((\w+)\)', r'\1?.\2', result)

    # (4b) Unicode escape 디코딩 — AI가 \uXXXX로 한글을 작성하는 경우 실제 문자로 변환
    def _decode_unicode_escape(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)
    result = re.sub(r'\\u([0-9A-Fa-f]{4})', _decode_unicode_escape, result)

    # (5) import 경로 자동 보정 — component_path_map으로 잘못된 경로 수정
    if component_path_map and filepath.endswith((".tsx", ".jsx")):
        result = _fix_import_paths(result, component_path_map)

    # (6) 파일 타입 경계 안전장치 — .tsx/.jsx 파일에 /* FILE: 이후 CSS 등 이질적 코드가
    #     붙어있으면 해당 지점에서 잘라냄 (split 실패 잔여물 제거)
    if filepath.endswith((".tsx", ".jsx", ".ts", ".js")):
        file_tag_m = re.search(r'^/\*\s*FILE:\s*\S+', result, flags=re.MULTILINE)
        if file_tag_m:
            result = result[:file_tag_m.start()].rstrip()

    # (7) import 중복 제거
    seen_imports: set[str] = set()
    deduped_lines: list[str] = []
    for line in result.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") and stripped in seen_imports:
            continue  # 중복 import 스킵
        if stripped.startswith("import "):
            seen_imports.add(stripped)
        deduped_lines.append(line)
    result = "\n".join(deduped_lines)

    return result.strip()


def _fuzzy_match_component(name: str, cpm: dict[str, str], threshold: float = 0.7) -> str | None:
    """컴포넌트 이름을 cpm 키에서 퍼지 매칭. threshold 이상이면 반환."""
    best_name: str | None = None
    best_ratio = 0.0
    name_lower = name.lower()
    for key in cpm:
        ratio = SequenceMatcher(None, name_lower, key.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_name = key
    if best_ratio >= threshold and best_name is not None:
        return best_name
    return None


def _fix_import_paths(code: str, cpm: dict[str, str]) -> str:
    """import 문의 컴포넌트 경로를 실제 workspace 파일 경로로 보정.

    처리:
      - import Foo from '@/components/Foo'   → import Foo from '@/components/atoms/Foo'
      - import Foo from '@/components/ui/Foo' → import Foo from '@/components/atoms/Foo'
      - import { Foo, Bar } from '@/components' → 개별 import로 분리
      - 존재하지 않는 컴포넌트 → 퍼지 매칭 (>70%) 또는 TODO 주석 처리
    """
    lines = code.split("\n")
    new_lines = []

    for line in lines:
        stripped = line.strip()

        # 패턴 1: import ComponentName from '@/...'
        m = re.match(
            r"^(import\s+)(\w+)(\s+from\s+['\"])(@/\S+?)(['\"];?\s*)$",
            stripped,
        )
        if m:
            comp_name = m.group(2)
            if comp_name in cpm:
                correct_path = cpm[comp_name]
                new_lines.append(f"{m.group(1)}{comp_name}{m.group(3)}{correct_path}{m.group(5)}")
                continue
            # 퍼지 매칭: cpm에 없지만 @/components/ 경로인 경우
            import_path = m.group(4)
            if import_path.startswith("@/components"):
                fuzzy = _fuzzy_match_component(comp_name, cpm)
                if fuzzy:
                    new_lines.append(f"{m.group(1)}{fuzzy}{m.group(3)}{cpm[fuzzy]}{m.group(5)}")
                    continue
                else:
                    # 매칭 불가 → TODO 주석 처리
                    new_lines.append(f"// TODO: component '{comp_name}' not found — {stripped}")
                    continue

        # 패턴 2: import { A, B, C } from '@/components' (배럴 import 분리)
        m2 = re.match(
            r"^import\s+\{\s*(.+?)\s*\}\s+from\s+['\"](@/components)['\"];?\s*$",
            stripped,
        )
        if m2:
            names = [n.strip() for n in m2.group(1).split(",") if n.strip()]
            replaced = False
            for name in names:
                resolved = name if name in cpm else _fuzzy_match_component(name, cpm)
                if resolved and resolved in cpm:
                    new_lines.append(f"import {resolved} from '{cpm[resolved]}';")
                    replaced = True
                else:
                    new_lines.append(f"// TODO: component '{name}' not found — import {name} from '@/components/{name}';")
                    replaced = True
            if replaced:
                continue

        # 패턴 3: import { A, B } from '@/components/...' (서브디렉토리 배럴)
        m3 = re.match(
            r"^import\s+\{\s*(.+?)\s*\}\s+from\s+['\"](@/components/\S+)['\"];?\s*$",
            stripped,
        )
        if m3:
            names = [n.strip() for n in m3.group(1).split(",") if n.strip()]
            all_in_map = all(n in cpm for n in names)
            if all_in_map and len(set(cpm[n] for n in names)) > 1:
                # 각각 다른 경로 → 분리
                for name in names:
                    new_lines.append(f"import {name} from '{cpm[name]}';")
                continue

        new_lines.append(line)

    return "\n".join(new_lines)


def _resolve_workspace_path(filepath: str, workspace_path: Path) -> Path:
    """파일 경로 → workspace 내 절대 경로 해석.

    라우팅 규칙:
      - src/app/*, src/pages/*, src/components/* → frontend/
      - src/routes/*, src/services/*, src/middleware/* → backend/
      - src/server.ts → backend/
      - prisma/* → backend/
      - *.css, *.tsx (app/ 하위) → frontend/
      - 나머지 → 경로 구조로 추론
    """
    fp = filepath.strip("/")

    # 명시적 frontend/backend 접두어
    if fp.startswith("frontend/") or fp.startswith("backend/"):
        return workspace_path / fp

    # Prisma → backend
    if fp.startswith("prisma/"):
        return workspace_path / "backend" / fp

    # src/ 하위 분류
    if fp.startswith("src/"):
        inner = fp[4:]  # src/ 이후

        # 백엔드 패턴
        be_prefixes = ("routes/", "services/", "middleware/", "modules/", "config/", "utils/server")
        if any(inner.startswith(p) for p in be_prefixes):
            return workspace_path / "backend" / fp
        if inner in ("server.ts", "app.ts", "index.ts") or inner.startswith("server"):
            return workspace_path / "backend" / fp

        # 프론트엔드 패턴
        fe_prefixes = ("app/", "pages/", "components/", "hooks/", "stores/", "styles/", "lib/")
        if any(inner.startswith(p) for p in fe_prefixes):
            return workspace_path / "frontend" / fp

        # 확장자 기반
        if fp.endswith((".tsx", ".jsx", ".css")):
            return workspace_path / "frontend" / fp
        if fp.endswith(".ts") and not fp.endswith(".test.ts"):
            # .ts는 import 패턴으로 추론 불가 → 백엔드 기본
            return workspace_path / "backend" / fp

    # SQL migration
    if fp.endswith(".sql"):
        return workspace_path / "backend" / fp

    # 폴백: 그대로
    return workspace_path / fp
