"""디자인 베이스라인 해결 — is_design_baseline=true 플래그 기반.

saver 가 HTML artifact 저장 직전에 본 모듈의 helper 를 호출하여 "어떤 산출물이
프로젝트 디자인 토큰의 source of truth 인가" 를 결정한다. 베이스라인 spec
(예: UI 디자인 시안) 의 artifact :root 블록을 다른 HTML 산출물의 기준으로 적용.

설계 원칙:
- 베이스라인 spec 은 `is_design_baseline: true` 로 명시 — 암묵적 "첫 artifact"
  방식은 재귀 오염 취약.
- 베이스라인 spec 명 목록은 프로세스 수명 동안 1회만 로드 (캐시).
- 베이스라인 artifact 가 없으면 enforce skip — 첫 HTML 생성 단계 보호.
- 베이스라인 자신이 저장 중이면 enforce skip — 자유 생성 허용.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]+\}", re.IGNORECASE | re.DOTALL)

# 프로세스 수명 캐시 — spec 파일 읽기는 1회만.
_baseline_names_cache: Optional[List[str]] = None


def get_baseline_spec_names() -> List[str]:
    """is_design_baseline=true 가 설정된 모든 spec 의 node name 목록 반환.

    spec yaml 의 `name:` 필드 (= DB 의 nodes.name) 를 수집.
    SkillRegistry.list_all() 은 파일 stem 만 반환하므로 여기서는 직접 yaml 파싱.
    프로세스 수명 동안 1회만 계산하고 캐시.
    """
    global _baseline_names_cache
    if _baseline_names_cache is not None:
        return _baseline_names_cache

    try:
        from pathlib import Path
        import yaml

        specs_dir = Path(__file__).resolve().parents[1] / "specs"
        names: List[str] = []
        for yaml_path in specs_dir.rglob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as parse_exc:
                logger.warning(
                    "baseline_yaml_parse_failed path=%s error=%s",
                    yaml_path.name, parse_exc,
                )
                continue
            if not isinstance(data, dict):
                continue
            if data.get("is_design_baseline") is True:
                nm = data.get("name") or yaml_path.stem
                if nm and nm not in names:
                    names.append(nm)
        _baseline_names_cache = names
        logger.info("design_baseline_names_loaded count=%d names=%s", len(names), names)
    except Exception as exc:
        logger.warning("design_baseline_names_load_failed error=%s", str(exc))
        _baseline_names_cache = []

    return _baseline_names_cache


def node_is_baseline(node_name: str, baseline_names: List[str]) -> bool:
    """해당 노드가 baseline spec 으로 생성되는지 판정."""
    if not node_name or not baseline_names:
        return False
    return node_name in baseline_names


async def load_baseline_root_block(
    db: Any,
    project_id: str,
    baseline_names: List[str],
    exclude_node_id: Optional[str] = None,
) -> Optional[str]:
    """프로젝트 내 가장 최근 베이스라인 artifact 의 :root 블록 반환.

    베이스라인 artifact 가 여러 개 (예: FO + BO 변형 모두) 있을 때는 최신
    `updated_at` 을 우선 — baseline 끼리는 팔레트가 동일한 것이 정상이며
    어느 하나를 기준으로 삼아도 결과는 같다.

    Returns:
        ":root { ... }" 문자열 또는 None (베이스라인 없거나 :root 미포함).
    """
    if not baseline_names:
        return None

    placeholders = ",".join(["?"] * len(baseline_names))
    params: List[Any] = [project_id] + list(baseline_names)
    exclude_clause = ""
    if exclude_node_id:
        exclude_clause = "AND a.node_id != ?"
        params.append(exclude_node_id)

    query = f"""
        SELECT av.storage_path
        FROM artifacts a
        JOIN artifact_versions av
          ON av.artifact_id = a.id
         AND av.version_num = a.current_version
        JOIN nodes n ON n.id = a.node_id
        WHERE a.project_id = ?
          AND a.artifact_type = 'html'
          AND n.name IN ({placeholders})
          {exclude_clause}
        ORDER BY a.updated_at DESC
        LIMIT 3
    """

    try:
        rows = await db.fetchall(query, tuple(params))
    except Exception as exc:
        logger.debug("baseline_query_failed error=%s", str(exc))
        return None

    for row in rows or []:
        content = row["storage_path"] if row else None
        if not content:
            continue
        m = _ROOT_BLOCK_RE.search(content)
        if m and m.group(0).count("--") >= 2:
            return m.group(0)
    return None
