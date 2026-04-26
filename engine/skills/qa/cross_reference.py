"""Pillar 3 — 페이지 레시피 ↔ 컴포넌트 라이브러리 cross-reference 자동 검증.

페이지 레시피의 placements.component_name 이 라이브러리 (composition_components)
에 정의되어 있는지 확인. 누락 컴포넌트 list 반환 → 라이브러리 retry 트리거 또는
NEEDS_HUMAN 처리에 활용.

Habit Tracker 사례: 레시피 37개 참조 vs 라이브러리 23개 → 18개 미정의 (51% 매칭).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CrossRefResult:
    """페이지 레시피 ↔ 라이브러리 정합성 측정 결과."""
    library_components: set[str] = field(default_factory=set)
    recipe_refs: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    matched: set[str] = field(default_factory=set)
    severity: str = "pass"   # pass | warn | fail
    suggested_action: str = ""

    @property
    def match_ratio(self) -> float:
        if not self.recipe_refs:
            return 1.0
        return len(self.matched) / len(self.recipe_refs)


# ============================================================
# 라이브러리 / 레시피 로딩
# ============================================================


async def load_library_components(db: Any, project_id: str) -> set[str]:
    """composition_components 테이블에서 모든 컴포넌트 name 로드."""
    rows = await db.fetchall(
        "SELECT name FROM composition_components WHERE project_id=?",
        (project_id,),
    )
    return {r["name"] for r in (rows or []) if r.get("name")}


async def load_recipe_component_refs(db: Any, project_id: str) -> set[str]:
    """composition_recipes 의 data JSON 에서 component_name 모두 추출."""
    rows = await db.fetchall(
        "SELECT data FROM composition_recipes WHERE project_id=?",
        (project_id,),
    )
    refs: set[str] = set()
    for r in (rows or []):
        data_str = r.get("data") or ""
        if not data_str:
            continue
        # JSON parse 시도 — 실패해도 regex fallback
        try:
            data = json.loads(data_str)
            refs.update(_walk_component_names(data))
        except json.JSONDecodeError:
            # regex fallback — JSON 깨졌어도 component_name 추출
            for m in re.finditer(r'"component_name"\s*:\s*"([^"]+)"', data_str):
                refs.add(m.group(1))
    return {r for r in refs if r}


def _walk_component_names(obj: Any) -> set[str]:
    """JSON tree 에서 component_name 키 모두 수집."""
    found: set[str] = set()
    if isinstance(obj, dict):
        if "component_name" in obj and isinstance(obj["component_name"], str):
            found.add(obj["component_name"])
        for v in obj.values():
            found.update(_walk_component_names(v))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_walk_component_names(item))
    return found


# ============================================================
# 정합성 검증 + 대응 제안
# ============================================================


async def verify_component_consistency(
    db: Any,
    project_id: str,
    fail_threshold: float = 0.95,
    warn_threshold: float = 0.80,
) -> CrossRefResult:
    """페이지 레시피 ↔ 라이브러리 정합성 측정.

    Args:
        fail_threshold: 매칭 비율 미만 시 severity=fail
        warn_threshold: 그 미만 시 severity=warn

    Returns:
        CrossRefResult — missing list + severity + 대응 제안
    """
    library = await load_library_components(db, project_id)
    refs = await load_recipe_component_refs(db, project_id)
    missing = refs - library
    matched = refs & library

    result = CrossRefResult(
        library_components=library,
        recipe_refs=refs,
        missing=missing,
        matched=matched,
    )
    if not refs:
        result.severity = "pass"
        result.suggested_action = "no_recipes"
        return result

    ratio = result.match_ratio
    if ratio >= fail_threshold:
        result.severity = "pass"
    elif ratio >= warn_threshold:
        result.severity = "warn"
        result.suggested_action = "review_missing"
    else:
        result.severity = "fail"
        result.suggested_action = "extend_library"

    logger.info(
        "cross_ref_check project=%s refs=%d matched=%d missing=%d ratio=%.2f severity=%s",
        project_id[:8], len(refs), len(matched), len(missing), ratio, result.severity,
    )
    if missing:
        logger.warning(
            "cross_ref_missing project=%s components=%s",
            project_id[:8], sorted(missing)[:10],
        )
    return result


# ============================================================
# 라이브러리 retry prompt (extend 명령)
# ============================================================


def build_library_extend_prompt(missing: set[str]) -> str:
    """라이브러리 retry 시 prompt 에 주입할 missing list 텍스트."""
    if not missing:
        return ""
    items = "\n".join(f"  - {name}" for name in sorted(missing))
    return (
        "## 누락 컴포넌트 보완 필수\n"
        "다음 컴포넌트들이 페이지 레시피에서 참조되지만 본 라이브러리에 정의 누락:\n"
        f"{items}\n\n"
        "위 컴포넌트들 모두 라이브러리에 추가 정의 — name(snake_case) / category / "
        "html_template / css 필수."
    )
