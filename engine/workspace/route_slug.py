"""Next.js App Router 동적 슬러그 충돌 방지 유틸 (SSOT).

모든 라우트 자동생성 지점(ui_completeness, page_builder 등)이 이 모듈만
호출하도록 통일. 같은 경로 안에서 [param] 세그먼트 이름이 중복되면
Next.js가 기동 자체를 거부한다:
  Error: You cannot have the same slug name "id" repeat within a single dynamic path.

주요 함수:
- normalize_dynamic_segment(parent, resource): 부모 경로에 동일 슬러그가
  이미 있으면 리소스명 기반 유니크 슬러그 반환. 없으면 기본 [id].
- detect_duplicate_slugs(app_root): src/app 하위를 walk하며 중복 슬러그
  보유 경로 목록 반환.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath


_SLUG_RE = re.compile(r"^\[(\.{3})?(\w+)\]$")


def _to_camel_case(text: str) -> str:
    """kebab/snake → camelCase. 'care-logs' → 'careLog', 'care_notes' → 'careNote'."""
    parts = re.split(r"[-_\s]+", text.strip())
    parts = [p for p in parts if p]
    if not parts:
        return "item"
    head = parts[0].lower()
    tail = "".join(p.capitalize() for p in parts[1:])
    camel = head + tail
    # 복수형 단순 단수화 (라우트 파라미터는 단수가 자연스러움)
    if camel.endswith("ies") and len(camel) > 3:
        camel = camel[:-3] + "y"
    elif camel.endswith("ses") or camel.endswith("xes"):
        camel = camel[:-2]
    elif camel.endswith("s") and not camel.endswith("ss"):
        camel = camel[:-1]
    return camel or "item"


def _collect_parent_slugs(parent: PurePath) -> set[str]:
    """부모 경로에 포함된 모든 [param] 세그먼트 이름 집합."""
    slugs: set[str] = set()
    for part in parent.parts:
        m = _SLUG_RE.match(part)
        if m:
            slugs.add(m.group(2))
    return slugs


def normalize_dynamic_segment(
    parent: PurePath | Path | str,
    resource: str,
    default: str = "id",
) -> str:
    """부모 경로를 보고 안전한 [slug] 세그먼트 이름을 반환.

    - 부모에 동일 슬러그가 없으면 `[id]` (default).
    - 이미 있으면 리소스명 기반 `[careLogId]` 같은 유니크 슬러그.
    - 그래도 충돌하면 접미사 2,3... 붙임.
    """
    parent_path = PurePath(str(parent))
    existing = _collect_parent_slugs(parent_path)

    if default not in existing:
        return f"[{default}]"

    base = _to_camel_case(resource) + "Id"
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}{n}"
        n += 1
    return f"[{candidate}]"


@dataclass
class DuplicateSlug:
    route_path: Path
    duplicated_slug: str
    occurrences: list[Path]  # 동일 슬러그가 등장한 각 디렉터리 경로


def detect_duplicate_slugs(app_root: Path) -> list[DuplicateSlug]:
    """src/app 하위를 walk하며 동일 경로 내 [slug] 중복 탐지.

    Next.js App Router 규칙: 같은 root → leaf 경로 안에서 [param] 이름
    유니크해야 함. page.tsx/page.jsx가 있는 리프만 체크.
    """
    if not app_root.exists():
        return []

    results: list[DuplicateSlug] = []
    for page in app_root.rglob("page.*"):
        if page.suffix not in {".tsx", ".ts", ".jsx", ".js"}:
            continue
        rel = page.parent.relative_to(app_root)
        seen: dict[str, list[Path]] = {}
        for idx, part in enumerate(rel.parts):
            m = _SLUG_RE.match(part)
            if not m:
                continue
            slug = m.group(2)
            dir_path = app_root / PurePath(*rel.parts[: idx + 1])
            seen.setdefault(slug, []).append(dir_path)
        for slug, occurrences in seen.items():
            if len(occurrences) > 1:
                results.append(
                    DuplicateSlug(
                        route_path=page.parent,
                        duplicated_slug=slug,
                        occurrences=occurrences,
                    )
                )
    return results


def suggest_rename(dup: DuplicateSlug) -> dict[Path, str]:
    """중복 슬러그 해결을 위한 rename 제안.

    첫 번째 occurrence는 유지, 두 번째부터는 부모 디렉터리 이름 기반
    유니크 슬러그로 rename 제안.
    """
    suggestions: dict[Path, str] = {}
    for occ in dup.occurrences[1:]:
        parent_dir_name = occ.parent.name  # e.g. "care-logs"
        new_slug = normalize_dynamic_segment(
            occ.parent.parent, parent_dir_name, default=dup.duplicated_slug
        )
        # new_slug는 이미 [] 포함. suggest_rename은 이름만 반환
        suggestions[occ] = new_slug
    return suggestions
