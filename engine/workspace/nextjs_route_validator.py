"""Next.js App Router 구조 검증기.

라우트 충돌(중복 [slug])을 프로그래매틱하게 검증하고, 자동 수정 제안을
반환한다. programmatic_verify와 harness가 공통으로 호출.

- validate_nextjs_routes(frontend_root): ValidationResult 반환.
- apply_rename_suggestions(suggestions): 실제 폴더 rename + 파일 내
  params.<old> -> params.<new> 치환.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from engine.workspace.route_slug import (
    DuplicateSlug,
    detect_duplicate_slugs,
    suggest_rename,
)


logger = logging.getLogger(__name__)


# LLM이 template literal을 리터럴로 박아버린 흔적 (e.g. "{data", "${user")
_TEMPLATE_LEAK_RE = re.compile(r"[\{\}\$`]")

# AI가 화면 이름 추론 실패해 남긴 플레이스홀더 (grp-01, group-02, unnamed 등)
_PLACEHOLDER_NAME_RE = re.compile(
    r"^(grp|group|page|screen|unnamed|undefined|tbd|todo)[-_]?\d*$",
    re.IGNORECASE,
)


@dataclass
class InvalidFolderName:
    path: Path
    reason: str  # 'template_leak' | 'placeholder_name' | 'invalid_char'


@dataclass
class DuplicateResource:
    """같은 리소스를 2개 이상의 경로로 구현한 것 (admin-x vs admin/x)."""
    resource: str
    paths: list[Path]


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    duplicates: list[DuplicateSlug] = field(default_factory=list)
    invalid_folders: list[InvalidFolderName] = field(default_factory=list)
    duplicate_resources: list[DuplicateResource] = field(default_factory=list)
    # occurrence_path -> new_slug_name (with brackets)
    rename_suggestions: dict[Path, str] = field(default_factory=dict)


def _locate_app_dir(frontend_root: Path) -> Path | None:
    """frontend_root에서 Next.js app 디렉터리 위치를 찾는다 (src/app 또는 app)."""
    for candidate in (frontend_root / "src" / "app", frontend_root / "app"):
        if candidate.is_dir():
            return candidate
    return None


def _detect_invalid_folder_names(app_dir: Path) -> list[InvalidFolderName]:
    """src/app 하위에서 LLM 생성 오류로 잘못 이름붙은 폴더 탐지.

    - template leak: {, }, $, ` 문자를 포함 (e.g. `{data`, `${user`)
    - placeholder name: grp-01, screen-02, unnamed 등 이름 추론 실패 잔재
    - 공백 포함
    """
    invalid: list[InvalidFolderName] = []
    for sub in app_dir.rglob("*"):
        if not sub.is_dir():
            continue
        name = sub.name
        # Next.js route group "(main)" 과 dynamic segment "[id]"는 제외
        if name.startswith("(") and name.endswith(")"):
            continue
        if name.startswith("[") and name.endswith("]"):
            # 내부에도 템플릿 리크 체크
            inner = name.strip("[]").lstrip(".")
            if _TEMPLATE_LEAK_RE.search(inner) or " " in inner:
                invalid.append(InvalidFolderName(sub, "invalid_char"))
            continue
        if _TEMPLATE_LEAK_RE.search(name):
            invalid.append(InvalidFolderName(sub, "template_leak"))
            continue
        if " " in name:
            invalid.append(InvalidFolderName(sub, "invalid_char"))
            continue
        if _PLACEHOLDER_NAME_RE.match(name):
            invalid.append(InvalidFolderName(sub, "placeholder_name"))
    return invalid


def _canonicalize_route_name(name: str) -> str:
    """`admin-caregivers` ↔ `admin/caregivers`를 동일 키로 수렴."""
    # Next.js route group 괄호 제거
    if name.startswith("(") and name.endswith(")"):
        return ""
    # dash/slash/underscore 모두 '/'로
    return name.replace("-", "/").replace("_", "/").lower().strip("/")


def _detect_duplicate_resources(app_dir: Path) -> list[DuplicateResource]:
    """동일 리소스를 2개 이상 경로로 구현한 것 탐지.

    예: /admin-caregivers/page.tsx 와 /admin/caregivers/page.tsx 둘 다 존재.
    """
    from collections import defaultdict
    resource_map: dict[str, list[Path]] = defaultdict(list)
    for page in app_dir.rglob("page.*"):
        if page.suffix not in {".tsx", ".ts", ".jsx", ".js"}:
            continue
        rel = page.parent.relative_to(app_dir)
        # route group, dynamic segment 제거 후 정규화
        parts = []
        for p in rel.parts:
            canon = _canonicalize_route_name(p)
            if canon and not p.startswith("["):
                parts.append(canon)
        key = "/".join(parts)
        if not key:
            continue
        resource_map[key].append(page.parent)

    duplicates: list[DuplicateResource] = []
    for key, paths in resource_map.items():
        if len(paths) > 1:
            duplicates.append(DuplicateResource(resource=key, paths=paths))
    return duplicates


def validate_nextjs_routes(frontend_root: Path | str) -> ValidationResult:
    """프론트엔드 루트를 받아 라우트 구조 검증.

    검사 항목:
    1. 동일 경로 내 [slug] 중복 (Next.js 기동 직격)
    2. 유효하지 않은 폴더명 (template leak, placeholder, 공백)
    3. 동일 리소스 중복 구현 (admin-x vs admin/x)
    """
    root = Path(frontend_root)
    app_dir = _locate_app_dir(root)
    if app_dir is None:
        return ValidationResult(ok=True, errors=[])  # app 디렉터리 없으면 스킵

    duplicates = detect_duplicate_slugs(app_dir)
    invalid_folders = _detect_invalid_folder_names(app_dir)
    duplicate_resources = _detect_duplicate_resources(app_dir)

    errors: list[str] = []
    all_suggestions: dict[Path, str] = {}

    for dup in duplicates:
        errors.append(
            f"duplicate_slug=[{dup.duplicated_slug}] at {dup.route_path.relative_to(app_dir)}"
        )
        all_suggestions.update(suggest_rename(dup))

    for inv in invalid_folders:
        errors.append(
            f"invalid_folder={inv.reason} at {inv.path.relative_to(app_dir)}"
        )

    for dr in duplicate_resources:
        rel_paths = [str(p.relative_to(app_dir)) for p in dr.paths]
        errors.append(f"duplicate_resource={dr.resource} paths={rel_paths}")

    ok = (not duplicates) and (not invalid_folders) and (not duplicate_resources)

    return ValidationResult(
        ok=ok,
        errors=errors,
        duplicates=duplicates,
        invalid_folders=invalid_folders,
        duplicate_resources=duplicate_resources,
        rename_suggestions=all_suggestions,
    )


def _strip_brackets(slug: str) -> str:
    """[careLogId] -> careLogId, [...slug] -> slug."""
    inner = slug.strip("[]")
    if inner.startswith("..."):
        inner = inner[3:]
    return inner


def _update_params_refs(file: Path, old_name: str, new_name: str) -> int:
    """파일 내 params.<old> -> params.<new> 치환. 치환 건수 반환.

    정규식은 Next.js 관례적 사용 패턴만 매치하여 과잉 치환 방지.
    """
    try:
        text = file.read_text(encoding="utf-8")
    except Exception:
        return 0

    # `params.id` 혹은 `params["id"]`, `params: { id: string }` 등 주요 패턴
    patterns = [
        (rf"(\bparams\s*\.\s*){re.escape(old_name)}\b", rf"\g<1>{new_name}"),
        (
            rf"(\bparams\s*\[\s*[\"']){re.escape(old_name)}([\"']\s*\])",
            rf"\g<1>{new_name}\g<2>",
        ),
        (
            rf"(\bparams\s*:\s*\{{\s*){re.escape(old_name)}(\s*[:;])",
            rf"\g<1>{new_name}\g<2>",
        ),
        (
            rf"(\{{\s*){re.escape(old_name)}(\s*\}}\s*=\s*params\b)",
            rf"\g<1>{new_name}\g<2>",
        ),
    ]

    new_text = text
    total = 0
    for pat, repl in patterns:
        new_text, count = re.subn(pat, repl, new_text)
        total += count

    if total > 0:
        file.write_text(new_text, encoding="utf-8")
    return total


def apply_invalid_folder_cleanup(
    invalid: list[InvalidFolderName],
    app_dir: Path,
    dry_run: bool = False,
) -> list[tuple[Path, str]]:
    """유효하지 않은 폴더 자동 정리.

    - template_leak: 폴더명에서 `{`,`}`,`$`,` ` 제거한 이름으로 rename 시도.
      정리 결과 이름이 비거나 이미 존재하면 해당 폴더 트리 삭제.
    - placeholder_name: 다른 경로에 정식 구현이 있으면 삭제, 아니면 유지하되
      warning (안전 우선).
    - invalid_char: template_leak과 동일 처리.

    Returns: [(path, action), ...] action = 'removed' | 'renamed_to:<new>' | 'kept_warn'.
    """
    import shutil as _shutil
    actions: list[tuple[Path, str]] = []

    # 깊은 경로부터 처리
    ordered = sorted(invalid, key=lambda i: len(i.path.parts), reverse=True)

    for inv in ordered:
        if not inv.path.is_dir():
            continue

        if inv.reason in {"template_leak", "invalid_char"}:
            cleaned = _TEMPLATE_LEAK_RE.sub("", inv.path.name).strip().strip("-_/")
            if not cleaned or cleaned == inv.path.name:
                # 정리 후 빈 이름 → 트리 삭제
                if not dry_run:
                    _shutil.rmtree(inv.path)
                actions.append((inv.path, "removed"))
                continue
            new_path = inv.path.parent / cleaned
            if new_path.exists():
                # 대상이 이미 있음 → 중복이므로 잘못된 쪽 삭제
                if not dry_run:
                    _shutil.rmtree(inv.path)
                actions.append((inv.path, "removed"))
            else:
                if not dry_run:
                    _shutil.move(str(inv.path), str(new_path))
                actions.append((inv.path, f"renamed_to:{cleaned}"))
            continue

        if inv.reason == "placeholder_name":
            # 동일 이름이 app_dir 다른 곳에 정식으로 있으면 삭제, 없으면 kept_warn
            # (이름만 보고 판단하기 어려워 보수적으로 keep)
            actions.append((inv.path, "kept_warn"))

    return actions


def apply_duplicate_resource_cleanup(
    duplicates: list[DuplicateResource],
    app_dir: Path,
    dry_run: bool = False,
) -> list[tuple[Path, str]]:
    """중복 리소스 경로 중 표준형을 유지하고 비표준 삭제.

    표준형 판단 기준 (우선순위):
    1. 경로 깊이가 더 깊은 쪽 (e.g. admin/caregivers > admin-caregivers)
    2. 대시 없는 쪽 (slash 기반 계층 구조)
    3. 알파벳 순

    Returns: [(removed_path, 'removed'), ...]
    """
    import shutil as _shutil
    actions: list[tuple[Path, str]] = []

    for dr in duplicates:
        # 표준형 선정
        ranked = sorted(
            dr.paths,
            key=lambda p: (
                -len(p.relative_to(app_dir).parts),  # 더 깊은 것 우선
                ("-" in p.name),                      # 대시 없는 것 우선
                str(p),
            ),
        )
        keeper = ranked[0]
        for victim in ranked[1:]:
            if not victim.is_dir():
                continue
            if not dry_run:
                _shutil.rmtree(victim)
            actions.append((victim, f"removed_duplicate_of:{keeper.relative_to(app_dir)}"))

    return actions


def apply_rename_suggestions(
    suggestions: dict[Path, str],
    app_dir: Path,
) -> list[tuple[Path, Path, int]]:
    """제안된 rename을 실제 파일시스템에 적용.

    Returns: [(old_dir, new_dir, params_refs_updated_count), ...]
    """
    applied: list[tuple[Path, Path, int]] = []

    # 깊은 경로부터 처리 (상위가 먼저 바뀌면 하위 경로가 깨짐)
    ordered = sorted(suggestions.items(), key=lambda kv: len(kv[0].parts), reverse=True)

    for old_dir, new_slug in ordered:
        if not old_dir.is_dir():
            logger.warning("rename_skip old_dir_missing=%s", old_dir)
            continue
        new_dir = old_dir.parent / new_slug
        if new_dir.exists():
            logger.warning(
                "rename_skip target_exists old=%s new=%s", old_dir, new_dir
            )
            continue

        # params.<old> -> params.<new> 치환 (rename 전, 하위 모든 tsx/ts/jsx/js)
        old_param = _strip_brackets(old_dir.name)
        new_param = _strip_brackets(new_slug)
        refs = 0
        for f in old_dir.rglob("*"):
            if f.suffix in {".tsx", ".ts", ".jsx", ".js"}:
                refs += _update_params_refs(f, old_param, new_param)

        # 다른 페이지에서 이 경로를 import/push 하는 경우도 갱신 (보수적 범위)
        for f in app_dir.rglob("*"):
            if f.is_file() and f.suffix in {".tsx", ".ts", ".jsx", ".js"}:
                if f.is_relative_to(old_dir):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                # href/Link에 하드코딩된 old_param 치환은 위험하므로 생략.
                # params 참조만 안전하게 갱신.
                if old_param in text and f"params.{old_param}" in text:
                    refs += _update_params_refs(f, old_param, new_param)

        shutil.move(str(old_dir), str(new_dir))
        logger.info(
            "route_slug_renamed old=%s new=%s params_refs=%d",
            old_dir, new_dir, refs,
        )
        applied.append((old_dir, new_dir, refs))

    return applied
