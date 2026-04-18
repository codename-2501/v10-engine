"""
Artifact loaders — DB query helpers extracted from executor.py (Phase 3).

All ``_load_*`` functions that fetch project/artifact/design data from the
database live here so that executor.py stays focused on orchestration.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

from engine.ai.context_assembler import (
    DeltaEntry,
    ProjectContext,
)
from engine.core.dag_advancer import NodeSnapshot
from engine.skills.codegen.frontend_infra import _design_tokens_to_css
from engine.skills.utils import _now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project context
# ---------------------------------------------------------------------------

async def _load_project_context(db: Any, project_id: str) -> ProjectContext:
    """Fetch project metadata from the database.

    Args:
        db:         Async DB connection.
        project_id: UUID of the project.

    Returns:
        A ``ProjectContext`` dataclass populated from the DB row.
    """
    # 실제 projects 스키마: id, name, client_name, project_type,
    #   global_context (TEXT/JSON), phase, engagement_id, backend_choice (036)
    # backend_choice 는 migration 036에서 추가되었으므로 COALESCE 로 이전 호환
    row = await db.fetchone(
        "SELECT id, name, client_name, project_type, "
        "global_context, phase, engagement_id, "
        "COALESCE(backend_choice, 'sql') AS backend_choice "
        "FROM projects WHERE id = ?",
        (project_id,),
    )
    if row is None:
        return ProjectContext(
            project_id=project_id,
            project_name="Unknown",
            client_name="Unknown",
            project_type="Unknown",
            global_context={},
            phase="",
            engagement_id="",
        )

    # global_context: DB TEXT → dict 변환
    raw_ctx = row["global_context"]
    if isinstance(raw_ctx, dict):
        global_ctx = raw_ctx
    else:
        try:
            global_ctx = json.loads(raw_ctx) if raw_ctx else {}
        except (ValueError, TypeError):
            global_ctx = {}

    # Phase F-4: backend_choice를 global_context에 주입
    # (ProjectContext는 코어라 수정 불가 → global_context 경유)
    try:
        global_ctx["backend_choice"] = row["backend_choice"] or "sql"
    except (KeyError, TypeError):
        global_ctx.setdefault("backend_choice", "sql")

    return ProjectContext(
        project_id=row["id"],
        project_name=row["name"],
        client_name=row["client_name"],
        project_type=row["project_type"],
        global_context=global_ctx,
        phase=row["phase"] or "",
        engagement_id=row["engagement_id"] or "",
    )


# ---------------------------------------------------------------------------
# Task / file-manifest artifact loading
# ---------------------------------------------------------------------------

async def _load_task_artifact(
    db: Any, task_node_id: Optional[str], qa_mode: bool = False
) -> Optional[str]:
    """Fetch the artifact content produced by a paired TASK node.

    Used by QA nodes to run programmatic validation before calling the LLM.

    file_manifest가 있으면 워크스페이스 파일을 직접 읽어서 컨텍스트 구성:
      - 필수 파일(globals.css, layout.tsx, 주요 컴포넌트): 전체 읽기
      - 나머지 파일: 파일명 + 첫 30줄만 (exports 확인용)
    file_manifest가 없으면 기존 방식(storage_path 내용) 유지.

    Args:
        db:           Async DB connection.
        task_node_id: The node ID of the paired TASK node, or ``None``.
        qa_mode:      True면 QA 전용 낮은 예산 적용 (5K/1.2K/12줄).
                      구조·exports 판정에 충분하며 토큰을 ~85% 절약.

    Returns:
        The artifact content string, or ``None`` if not found.
    """
    if not task_node_id:
        return None
    row = await db.fetchone(
        "SELECT av.storage_path AS content, av.file_manifest FROM artifact_versions av "
        "JOIN artifacts a ON a.id = av.artifact_id WHERE a.node_id = ? "
        "AND av.version_num = a.current_version",
        (task_node_id,),
    )
    if not row:
        return None

    # file_manifest가 있으면 워크스페이스 파일 직접 읽기
    if row["file_manifest"]:
        workspace_content = await _load_files_from_manifest(
            db, task_node_id, row["file_manifest"], qa_mode=qa_mode
        )
        if workspace_content:
            return workspace_content
        # 파일 읽기 실패 시 기존 storage_path 내용으로 폴백
        logger.warning(
            "file_manifest_read_failed task_node_id=%s — falling back to storage_path",
            task_node_id,
        )

    content = row["content"] or ""
    # qa_mode: HTML 산출물은 CSS+body 구조 파악에 충분한 버퍼 필요
    #   - HTML: 24K (CSS 16K + body 콘텐츠 8K 이상 확보)
    #   - 텍스트/JSON: 4K (구조 확인에 충분)
    # non-qa_mode: manifest 경로와 동일 예산 40K 캡 (무제한 방지)
    if qa_mode and content:
        _is_html = content.lstrip()[:20].lower().startswith(("<!doctype", "<html", "<head", "<style"))
        _qa_cap = 24_000 if _is_html else 4_000
        return content[:_qa_cap]
    if content:
        return content[:40000]
    return None


async def _load_files_from_manifest(
    db: Any, task_node_id: str, file_manifest_json: str,
    qa_mode: bool = False,
) -> Optional[str]:
    """file_manifest의 파일 목록을 워크스페이스에서 직접 읽어 컨텍스트 구성.

    전략:
      - 필수 파일(globals.css, layout.tsx, page.tsx, 주요 컴포넌트): 전체 내용
      - 나머지 파일: 파일명 + 첫 30줄 (exports/props 확인용)

    Args:
        db:                 Async DB connection (workspace 경로 조회용).
        task_node_id:       TASK 노드 ID (프로젝트 정보 조회용).
        file_manifest_json: JSON 배열 문자열 ["path/to/file.tsx", ...]
        qa_mode:            True면 QA 전용 낮은 예산 적용.
                            MAX_TOTAL=5K / priority=1.2K / preview=12줄.
                            exports·구조 판정에 충분하며 토큰 ~85% 절약.

    Returns:
        파일 내용으로 구성된 컨텍스트 문자열, 또는 None.
    """
    import os as _os
    import json as _json_fm

    try:
        file_paths = _json_fm.loads(file_manifest_json)
    except (ValueError, TypeError):
        return None

    if not file_paths:
        return None

    # 프로젝트의 워크스페이스 경로 조회 (workspace_deployments 테이블)
    workspace_row = await db.fetchone(
        """SELECT wd.workspace_path FROM nodes n
           JOIN dags d ON d.id = n.dag_id
           JOIN workspace_deployments wd ON wd.project_id = d.project_id
           WHERE n.id = ? AND wd.workspace_path IS NOT NULL AND wd.workspace_path != ''
           LIMIT 1""",
        (task_node_id,),
    )
    workspace_path = workspace_row["workspace_path"] if workspace_row and workspace_row.get("workspace_path") else None

    if not workspace_path:
        logger.debug("workspace_path_not_found task_node_id=%s", task_node_id)
        return None

    # 필수 파일 패턴 (전체 내용 읽기)
    PRIORITY_PATTERNS = (
        "globals.css",
        "layout.tsx",
        "layout.jsx",
        "page.tsx",
        "page.jsx",
        "providers.tsx",
        "providers.jsx",
        "/ui/",           # 공통 UI 컴포넌트
        "Header",
        "Sidebar",
        "Footer",
        "Navigation",
    )

    if qa_mode:
        # QA 전용: 파일 수에 따라 예산 조정
        # 파일 20+개(프론트엔드 공통 인프라 등): 15K — 구조+핵심 파일 충분 확인
        # 파일 소수: 5K — exports 판정에 충분
        if len(file_paths) >= 10:
            MAX_TOTAL_CHARS = 15000
            MAX_PRIORITY_CHARS = 3000
            MAX_PREVIEW_LINES = 20
            mode_label = "QA 검증용 (파일 다수 — 확장 예산)"
        else:
            MAX_TOTAL_CHARS = 5000
            MAX_PRIORITY_CHARS = 1200
            MAX_PREVIEW_LINES = 12
            mode_label = "QA 검증용 요약"
    else:
        MAX_TOTAL_CHARS = 40000   # TASK 컨텍스트 최대 40K자
        MAX_PRIORITY_CHARS = 5000  # 필수 파일 1개당 최대 5K자
        MAX_PREVIEW_LINES = 30     # 일반 파일 미리보기 줄 수
        mode_label = "워크스페이스에서 직접 로드"

    parts = [
        f"\n\n## 구현 파일 목록 ({mode_label})",
        f"총 {len(file_paths)}개 파일. 필수 파일은 전체, 나머지는 첫 {MAX_PREVIEW_LINES}줄 미리보기.\n",
    ]
    total_chars = sum(len(p) for p in parts)
    files_read = 0
    files_preview = 0
    files_missing = 0

    for file_path in file_paths:
        if total_chars >= MAX_TOTAL_CHARS:
            remaining = len(file_paths) - files_read - files_preview - files_missing
            if remaining > 0:
                parts.append(f"\n... (나머지 {remaining}개 파일 생략 — 컨텍스트 한도 초과)")
            break

        # 절대 경로 구성
        abs_path = _os.path.join(workspace_path, file_path.lstrip("/"))

        is_priority = any(pat in file_path for pat in PRIORITY_PATTERNS)

        try:
            if not _os.path.exists(abs_path):
                parts.append(f"\n### {file_path} ⚠️ (파일 없음)")
                files_missing += 1
                total_chars += len(file_path) + 30
                continue

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                if is_priority:
                    # 필수 파일: 전체 읽기 (최대 MAX_PRIORITY_CHARS)
                    file_content = f.read(MAX_PRIORITY_CHARS)
                    is_truncated = _os.path.getsize(abs_path) > MAX_PRIORITY_CHARS
                    label = "전체" if not is_truncated else f"앞 {MAX_PRIORITY_CHARS}자"
                    parts.append(f"\n### {file_path} ({label})")
                    parts.append(f"```\n{file_content}\n```")
                    total_chars += len(file_content) + len(file_path) + 20
                    files_read += 1
                else:
                    # 일반 파일: 첫 30줄 미리보기
                    lines = []
                    for _ in range(MAX_PREVIEW_LINES):
                        line = f.readline()
                        if not line:
                            break
                        lines.append(line.rstrip("\n"))
                    preview = "\n".join(lines)
                    parts.append(f"\n### {file_path} (첫 {MAX_PREVIEW_LINES}줄 미리보기)")
                    parts.append(f"```\n{preview}\n```")
                    total_chars += len(preview) + len(file_path) + 20
                    files_preview += 1

        except OSError as e:
            parts.append(f"\n### {file_path} ⚠️ (읽기 실패: {e})")
            files_missing += 1
            total_chars += len(file_path) + 40

    logger.info(
        "file_manifest_loaded task_node_id=%s total=%d read=%d preview=%d missing=%d chars=%d",
        task_node_id, len(file_paths), files_read, files_preview, files_missing, total_chars,
    )

    if files_read + files_preview == 0:
        return None  # 읽은 파일이 없으면 폴백

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------

async def _load_deltas(db: Any, node: NodeSnapshot) -> List[DeltaEntry]:
    """Fetch upstream change deltas for context assembly.

    Args:
        db:   Async DB connection.
        node: The current node snapshot.

    Returns:
        A list of ``DeltaEntry`` objects (may be empty).
    """
    rows = await db.fetchall(
        """SELECT d.artifact_id, a.artifact_type,
                  d.from_version_num, d.to_version_num,
                  d.delta_content, d.is_empty
           FROM deltas d
           JOIN artifacts a ON a.id = d.artifact_id
           WHERE d.impacted_node_ids LIKE ?""",
        (f"%{node.id}%",),
    )
    results: List[DeltaEntry] = []
    for row in rows:
        results.append(DeltaEntry(
            artifact_id=row["artifact_id"],
            artifact_type=row["artifact_type"] or "",
            from_version=row["from_version_num"],
            to_version=row["to_version_num"],
            diff=row["delta_content"] or "",
            is_empty=bool(row["is_empty"]),
        ))
    return results


# ---------------------------------------------------------------------------
# BUILD/DELIVER phase: 환경변수 로드
# ---------------------------------------------------------------------------

async def _load_env_vars_context(db, project_id: str, engagement_id: str) -> str:
    """프로젝트 환경변수를 프롬프트에 주입.

    3계층 폴백: PROJECT → ENGAGEMENT → GLOBAL.
    비밀 값은 마스킹 없이 전달 (AI가 코드에 사용해야 하므로).
    단, 실제 값 대신 환경변수 참조 패턴으로 사용하도록 지시.

    Returns:
        프롬프트에 추가할 문자열.
    """
    from engine.core.resource_resolver import ResourceResolver
    resolver = ResourceResolver(db)

    # 3계층에서 모든 키 수집
    all_keys = set()
    for scope, scope_id in [("PROJECT", project_id), ("ENGAGEMENT", engagement_id), ("GLOBAL", "GLOBAL")]:
        rows = await db.fetchall(
            "SELECT key FROM project_env_vars WHERE scope=? AND scope_id=?",
            (scope, scope_id),
        )
        for r in rows:
            all_keys.add(r["key"])

    if not all_keys:
        return ""

    # 각 키의 최종 값 조회 (폴백 적용) — 값이 있는 키와 없는 키 모두 수집
    configured_pairs: dict[str, str] = {}
    unconfigured_keys: list[str] = []
    for key in sorted(all_keys):
        try:
            val = await resolver.resolve(key, project_id, engagement_id)
            if val:
                configured_pairs[key] = val
            else:
                unconfigured_keys.append(key)
        except Exception as exc:
            logger.debug("env_resolve_failed key=%s error=%s", key, exc)
            unconfigured_keys.append(key)

    if not configured_pairs and not unconfigured_keys:
        return ""

    parts = [
        "\n\n---",
        "## 프로젝트 환경 변수",
        "아래 환경 변수를 코드에서 사용하세요.",
        "**중요: 값을 하드코딩하지 말고 `process.env.KEY` 또는 `os.environ['KEY']` 패턴으로 참조하세요.**\n",
    ]

    if configured_pairs:
        parts.append("### 설정 완료 (값 사용 가능)")
        for key, val in configured_pairs.items():
            is_secret = key.upper().endswith(("KEY", "SECRET", "TOKEN", "PASSWORD", "PASS"))
            if is_secret:
                parts.append(f"- `{key}` = (설정됨 — 환경변수로 참조)")
            else:
                parts.append(f"- `{key}` = `{val[:50]}`")

    if unconfigured_keys:
        parts.append("\n### 미설정 (환경변수 참조 패턴으로 구현)")
        parts.append("아래 키는 아직 값이 설정되지 않았지만, 환경변수 참조 패턴(`process.env.KEY` / `os.environ['KEY']`)으로 코드를 구현하세요.")
        parts.append("런타임에 값이 주입됩니다.\n")
        for key in unconfigured_keys:
            parts.append(f"- `{key}`")

    parts.append("\n코드에서 직접 값을 넣지 말고 환경변수 참조 패턴을 사용하세요.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# DESIGN artifacts for BUILD
# ---------------------------------------------------------------------------

async def _load_design_artifacts_for_build(db, node) -> str:
    """BUILD 노드 실행 시 DESIGN 산출물을 프롬프트에 주입.

    디자인 토큰 JSON → CSS 변수 블록으로 변환하여 AI가 정확히 따르도록 강제.
    UI 시안, 컴포넌트 정의서, 화면 설계서도 포함 (토큰 예산 내에서).

    Returns:
        프롬프트에 추가할 문자열.
    """
    rows = await db.fetchall(
        """SELECT n.name, av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.phase = 'DESIGN'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           ORDER BY n.name""",
        (node.project_id,),
    )

    if not rows:
        return ""

    parts = [
        "\n\n## ⚠️ DESIGN 산출물 — 반드시 이 디자인을 그대로 구현하라 (임의 변경 금지)",
    ]

    # 디자인 토큰 우선 처리 — JSON을 CSS 변수 블록으로 변환
    token_css = ""
    for r in rows:
        if "디자인 토큰" in r["name"]:
            try:
                tokens = json.loads(r["content"])
                token_css = _design_tokens_to_css(tokens)
                parts.append(f"\n### 디자인 토큰 (CSS 변수 — 이 값을 globals.css에 그대로 사용)")
                parts.append(f"```css\n{token_css}\n```")
                # 메타 정보 강조
                meta = tokens.get("meta", {})
                if meta.get("theme"):
                    parts.append(f"\n**테마: {meta['theme']}** — 이 테마를 반드시 따를 것")
                if meta.get("tone"):
                    parts.append(f"**디자인 톤: {meta['tone']}**")
                font = tokens.get("typography", {}).get("font_family", "")
                if font:
                    parts.append(f"**폰트: {font}** — 이 폰트를 사용할 것 (다른 폰트 금지)")
            except (json.JSONDecodeError, TypeError):
                parts.append(f"\n### 디자인 토큰 (raw)\n{r['content'][:3000]}")

    # 노드 이름에 따라 우선순위를 다르게 설정 — 필요한 설계 산출물만 주입
    # 변경 전: 모든 노드에 동일한 priority_order 적용 (불필요한 산출물 주입)
    # 변경 후: 노드별 최적화된 우선순위로 MAX_DESIGN_CHARS를 효율적으로 사용
    node_name_for_priority = getattr(node, "name", "") if node is not None else ""

    if "프론트엔드 공통 인프라" in node_name_for_priority:
        # 공통 인프라(globals.css, 레이아웃, 공통 컴포넌트 생성):
        # 컴포넌트 정의서 + 디자인 토큰만 필요 (화면 설계서 불필요)
        priority_order = [
            "컴포넌트 정의서",   # 컴포넌트 구조/네이밍/Props 정의
            "IA",               # 정보 구조 (라우팅/네비게이션)
            "상태 정의서",       # 전역 상태 구조
        ]
    elif "프론트엔드 컴포넌트 구현" in node_name_for_priority:
        # 컴포넌트 구현 (페이지 React 컴포넌트):
        # 화면 설계서 + UI 디자인 시안만 필요 (컴포넌트 정의서는 이미 공통 인프라에서 사용됨)
        priority_order = [
            "화면 설계서",       # 와이어프레임/화면 구성
            "UI 디자인 시안",    # 최종 시각 디자인
            "상태 정의서",       # 페이지별 상태 처리
        ]
    else:
        # 나머지 노드: 기존 방식 유지
        priority_order = [
            "컴포넌트 정의서",  # 컴포넌트 구조/네이밍
            "화면 설계서",      # 와이어프레임
            "UI 디자인 시안",   # 최종 시각 디자인
            "IA",              # 정보 구조
            "컴포넌트 라이브러리",
            "페이지 레시피",
            "상태 정의서",
        ]

    total_chars = sum(len(p) for p in parts)
    MAX_DESIGN_CHARS = 30000  # 디자인 컨텍스트 최대 30K자

    for priority_name in priority_order:
        for r in rows:
            if priority_name in r["name"] and "디자인 토큰" not in r["name"]:
                content = r["content"] or ""
                remaining = MAX_DESIGN_CHARS - total_chars
                if remaining <= 500:
                    break
                truncated = content[:remaining]
                parts.append(f"\n### {r['name']}")
                parts.append(truncated)
                total_chars += len(truncated) + len(r["name"]) + 10

    # 강제 규칙 주입
    parts.append("\n### ⚠️ 디자인 적용 필수 규칙")
    parts.append("1. 위 디자인 토큰의 CSS 변수를 globals.css :root에 그대로 복사할 것")
    parts.append("2. 위 디자인 토큰의 컬러를 임의로 변경하거나 다른 컬러 사용 금지")
    parts.append("3. 위 디자인 토큰의 폰트를 사용할 것 — 다른 폰트로 대체 금지")
    parts.append("4. UI 디자인 시안의 레이아웃, 간격, 그림자, 테두리 반경을 그대로 구현")
    parts.append("5. 디자인 시안에 다크 테마가 명시되어 있으면 다크 테마로 구현 (라이트 테마 금지)")
    parts.append("6. 인터랙션 프리미티브(Modal, ConfirmDialog, BottomSheet, Toast)는 공통 인프라에서 제공 — import해서 사용할 것")
    parts.append("7. 삭제/취소 등 위험 동작은 반드시 ConfirmDialog 사용, 성공/에러 피드백은 Toast 사용")

    logger.info(
        "design_artifacts_injected node_id=%s total_chars=%d token_css=%s",
        node.id, total_chars, "yes" if token_css else "no",
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Design compliance validation
# ---------------------------------------------------------------------------

async def _validate_design_compliance(db, node, task_output: str) -> list[str]:
    """BUILD QA 프로그래매틱 검증: TASK 산출물(코드)이 디자인 토큰을 준수하는지.

    디자인 토큰 JSON을 DB에서 로드 → 코드 내 CSS 변수/컬러값 비교.
    불일치 항목을 문자열 리스트로 반환 (비어있으면 통과).
    """
    issues = []

    # 디자인 토큰 로드
    token_row = await db.fetchone(
        """SELECT av.storage_path AS content FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.name = '디자인 토큰'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           LIMIT 1""",
        (node.project_id,),
    )
    if not token_row or not token_row["content"]:
        return []  # 디자인 토큰 없으면 검증 스킵

    try:
        tokens = json.loads(token_row["content"])
    except (json.JSONDecodeError, TypeError):
        return []

    colors = tokens.get("colors", {})
    meta = tokens.get("meta", {})
    typo = tokens.get("typography", {})

    # 1. 테마 불일치 검사
    design_theme = meta.get("theme", "").lower()
    if design_theme == "dark":
        # 코드에 라이트 테마 징후 체크
        light_indicators = [
            "#FFF9F5", "#FFFFFF", "#fff", "#fafafa", "#f5f5f5",
            "bg-white", "background: white", "background: #fff",
        ]
        for indicator in light_indicators:
            if indicator in task_output:
                issues.append(
                    f"[Major] 테마 불일치: 디자인 토큰은 dark 테마인데 코드에 라이트 배경({indicator}) 사용"
                )
                break
    elif design_theme == "light":
        dark_indicators = ["#111318", "#1a1d24", "#0d1117", "#161b22"]
        for indicator in dark_indicators:
            if indicator in task_output:
                issues.append(
                    f"[Major] 테마 불일치: 디자인 토큰은 light 테마인데 코드에 다크 배경({indicator}) 사용"
                )
                break

    # 2. 주요 컬러 불일치 검사
    design_bg = colors.get("bg", "")
    if design_bg and design_bg.startswith("#"):
        # globals.css나 코드에서 --bg 값 추출
        bg_match = re.search(r'--bg:\s*(#[0-9a-fA-F]{3,8})', task_output)
        if bg_match:
            impl_bg = bg_match.group(1).lower()
            design_bg_lower = design_bg.lower()
            if impl_bg != design_bg_lower:
                issues.append(
                    f"[Critical] 배경색 불일치: 디자인 토큰 --bg={design_bg} vs 구현 --bg={impl_bg}"
                )

    # 3. 폰트 불일치 검사
    design_font = typo.get("font_family", "")
    if design_font:
        font_lower = design_font.lower()
        if font_lower not in task_output.lower():
            # 코드에서 사용된 폰트 추출
            font_match = re.search(r"font-family:\s*['\"]?([^'\";\n,]+)", task_output)
            impl_font = font_match.group(1).strip() if font_match else "미지정"
            issues.append(
                f"[Major] 폰트 불일치: 디자인 토큰 font={design_font} vs 구현 font={impl_font}"
            )

    # 4. 화면 수 검사 (페이지 조립 vs 실제 코드)
    page_assembly = await db.fetchone(
        """SELECT av.storage_path AS content FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.name = '페이지 조립'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           LIMIT 1""",
        (node.project_id,),
    )
    if page_assembly and page_assembly["content"]:
        designed_pages = len(re.findall(r'^\|[^|]+\|', page_assembly["content"], re.MULTILINE)) - 1
        # 코드에서 page.tsx 개수 추정
        impl_pages = len(re.findall(r'// FILE:.*page\.tsx', task_output))
        if impl_pages == 0:
            impl_pages = task_output.count("page.tsx")
        if designed_pages > 0 and impl_pages > 0 and impl_pages < designed_pages * 0.6:
            issues.append(
                f"[Major] 화면 누락: 페이지 조립 {designed_pages}개 vs 구현 {impl_pages}개 ({impl_pages/designed_pages*100:.0f}%)"
            )

    if issues:
        logger.warning(
            "design_compliance_check node_id=%s issues=%d details=%s",
            node.id, len(issues), issues[:3],
        )

    return issues


# ---------------------------------------------------------------------------
# VERIFY phase: 상위 산출물 로드 (코드/설계 검증용)
# ---------------------------------------------------------------------------

async def _load_upstream_artifacts(db, node) -> str:
    """VERIFY 노드 실행 시 BUILD/DESIGN 산출물을 컨텍스트로 주입.

    AI가 실제 산출물을 읽고 검증할 수 있도록 상위 단계 산출물을 로드합니다.
    토큰 절약: 각 산출물 최대 3,000자, 전체 최대 15,000자.

    Returns:
        프롬프트에 추가할 문자열 (비어있으면 빈 문자열).
    """
    # retry_count > 0이면 이전에 결함이 발견된 것 → 수정된 부분만 로드
    if node.retry_count > 0:
        return await _load_defect_targets_only(db, node)

    # 첫 실행: 관련 산출물만 선별 로드 (노드 이름 기반 매칭)
    rows = await db.fetchall(
        """SELECT n.name, n.phase, av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.phase IN ('BUILD', 'DESIGN')
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           ORDER BY n.phase, n.name""",
        (node.project_id,),
    )

    if not rows:
        return ""

    # 테스트 노드 이름으로 관련 산출물 필터링
    # 예: "보안 취약점 점검" → 백엔드 API, 인증/인가 관련 우선
    # 예: "성능·부하 테스트" → 아키텍처, API, DB 관련 우선
    # 매칭 안 되면 DEVELOPMENT 전체 (코드 산출물은 꼭 봐야 함)
    code_nodes = [r for r in rows if r["phase"] == "BUILD"]
    design_nodes = [r for r in rows if r["phase"] == "DESIGN"]

    # DEVELOPMENT 코드는 필수, DESIGN은 아키텍처/API만
    selected = list(code_nodes)
    design_priority = ["아키텍처", "API", "ERD", "데이터 모델"]
    for d in design_nodes:
        if any(kw in d["name"] for kw in design_priority):
            selected.append(d)

    if not selected:
        selected = rows[:5]  # 폴백: 상위 5개

    parts = [
        "\n\n---",
        "## 검증 대상 산출물 (상위 단계)",
        "아래 산출물들을 **실제로 검토**하고, 결함/오류/누락을 구체적으로 보고하세요.",
        "결함이 없으면 '결함 없음'으로 명시하세요.\n",
    ]

    total_chars = 0
    max_total = 10000
    for row in selected:
        content = (row["content"] or "")[:3000]
        if not content or total_chars + len(content) > max_total:
            continue
        parts.append(f"### [{row['phase']}] {row['name']}")
        parts.append(f"```\n{content}\n```\n")
        total_chars += len(content)

    return "\n".join(parts)


async def _load_defect_targets_only(db, node) -> str:
    """재검증 시: 이전 결함에서 언급된 노드의 수정된 산출물만 로드."""
    # failure_reasons에서 어떤 phase/노드가 문제였는지 추출
    fr_row = await db.fetchone(
        "SELECT failure_reasons FROM nodes WHERE id=?", (node.id,)
    )
    if not fr_row or not fr_row["failure_reasons"]:
        return ""

    import json as _json
    try:
        reasons = _json.loads(fr_row["failure_reasons"])
    except (ValueError, TypeError):
        return ""

    if not reasons:
        return ""

    last_reason = reasons[-1].get("reason", "")

    # 수정된 상위 산출물만 로드 (INVALID → COMPLETED로 바뀐 것)
    rows = await db.fetchall(
        """SELECT n.name, n.phase, av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.phase IN ('BUILD', 'DESIGN')
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND a.current_version > 1
             AND av.version_num = a.current_version
           ORDER BY n.phase, n.name""",
        (node.project_id,),
    )

    if not rows:
        return ""

    MAX_DEFECT_TOTAL_CHARS = 10000

    parts = [
        "\n\n---",
        f"## 재검증 대상 (이전 결함: {last_reason[:100]})",
        "아래 **수정된** 산출물을 검토하고, 이전 결함이 해결되었는지 확인하세요.\n",
    ]
    total_chars = sum(len(p) for p in parts)
    included = 0

    for row in rows:
        content = (row["content"] or "")[:3000]
        if not content:
            continue
        block = f"### [수정됨] [{row['phase']}] {row['name']}\n```\n{content}\n```\n"
        if total_chars + len(block) > MAX_DEFECT_TOTAL_CHARS:
            remaining = len(rows) - included
            parts.append(f"\n... (나머지 {remaining}개 수정 산출물 목록만 표시)")
            for r in rows[included:]:
                parts.append(f"- [{r['phase']}] {r['name']}")
            break
        parts.append(block)
        total_chars += len(block)
        included += 1

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Infra summary / assembled pages
# ---------------------------------------------------------------------------

async def _load_infra_summary(db: Any, project_id: str) -> str:
    """프론트엔드 공통 인프라 산출물에서 컴포넌트 목록 + import 경로를 요약 추출."""
    infra_node = await db.fetchone(
        """SELECT n.id FROM nodes n JOIN dags d ON d.id=n.dag_id
           WHERE d.project_id=? AND n.name='프론트엔드 공통 인프라'
           AND n.node_type='TASK' AND n.state='COMPLETED' LIMIT 1""",
        (project_id,),
    )
    if not infra_node:
        return ""

    art = await db.fetchone(
        """SELECT av.storage_path AS content FROM artifact_versions av
           JOIN artifacts a ON a.id=av.artifact_id
           WHERE a.node_id=? AND av.version_num=a.current_version""",
        (infra_node["id"],),
    )
    if not art or not art["content"]:
        return ""

    # 공통 인프라 산출물에서 프로젝트 구조 + 컴포넌트 목록만 추출 (전체가 아닌 요약)
    content = art["content"]
    parts = [
        "\n\n## 공통 인프라 참조 (이전 단계에서 생성됨)",
        "아래 컴포넌트와 유틸리티를 import해서 사용하세요. 새로 만들지 마세요.",
    ]

    # 프로젝트 구조 섹션 추출 (5000자 한도)
    import re as _re_infra
    struct_match = _re_infra.search(
        r'(#{1,3}\s*프로젝트 구조[\s\S]*?)(?=#{1,3}\s|$)', content
    )
    if struct_match:
        parts.append(struct_match.group(1)[:3000])

    # globals.css 섹션 추출
    css_match = _re_infra.search(
        r'(#{1,3}\s*globals\.css[\s\S]*?)(?=#{1,3}\s|$)', content
    )
    if css_match:
        parts.append(css_match.group(1)[:5000])

    # 공통 컴포넌트 목록 (이름 + import 경로만)
    parts.append("\n### 사용 가능한 공통 컴포넌트")
    for comp_name in ["Button", "Input", "Modal", "Toast", "DataTable", "Card",
                       "StatCard", "Badge", "Loading", "EmptyState", "ErrorBoundary",
                       "Breadcrumb", "SearchBar", "Pagination", "Header", "Sidebar",
                       "Footer", "MobileBottomNav", "PageWrapper", "Textarea", "Select"]:
        if comp_name.lower() in content.lower():
            parts.append(f"- `{comp_name}` — import from '@/components/ui/{comp_name}'")

    return "\n".join(parts)


async def _load_assembled_pages(
    db: Any, project_id: str, page_slugs: list[str] | None = None,
) -> str:
    """페이지 조립 결과 HTML을 BUILD 프론트엔드 노드에 주입.

    page_slugs가 None이면 전체 페이지를 예산 배분 방식으로 로드.
    page_slugs가 제공되면 해당 페이지만 FULL HTML로 로드 (독립 노드용, 절단 없음).
    """
    import re as _re_ap

    assembly_node = await db.fetchone(
        """SELECT n.id FROM nodes n WHERE n.project_id=?
           AND n.name='페이지 조립' AND n.state='COMPLETED' LIMIT 1""",
        (project_id,),
    )
    if not assembly_node:
        return ""

    art = await db.fetchone(
        "SELECT id FROM artifacts WHERE node_id=? LIMIT 1",
        (assembly_node["id"],),
    )
    if not art:
        return ""

    versions = await db.fetchall(
        """SELECT storage_path FROM artifact_versions
           WHERE artifact_id=? AND storage_path LIKE '<!DOCTYPE%%'
           ORDER BY version_num""",
        (art["id"],),
    )

    # ── 필터 모드: 특정 page_slugs만 FULL HTML ──
    if page_slugs is not None:
        recipes = await db.fetchall(
            "SELECT page_slug, page_name FROM composition_recipes WHERE project_id=?",
            (project_id,),
        )
        name_to_slug = {r["page_name"]: r["page_slug"] for r in recipes}
        slug_set = set(page_slugs)

        MAX_PER_PAGE = 8000
        MAX_TOTAL = 64000
        parts = [f"\n\n## 조립 HTML 참조 ({len(page_slugs)}페이지 — FULL)\n"]
        total = 0

        for v in versions:
            html = v["storage_path"] or ""
            m = _re_ap.search(r"<title>(.+?)</title>", html, _re_ap.IGNORECASE)
            title = m.group(1).strip() if m else ""

            matched_slug = None
            for name, slug in name_to_slug.items():
                if name and name in title and slug in slug_set:
                    matched_slug = slug
                    break

            if not matched_slug:
                continue

            page_html = html[:MAX_PER_PAGE]
            if total + len(page_html) > MAX_TOTAL:
                parts.append(f"\n... (나머지 페이지 컨텍스트 한도 초과 — 생략)")
                break

            parts.append(f"\n### 페이지: {matched_slug}\n```html\n{page_html}\n```\n")
            total += len(page_html)
            slug_set.discard(matched_slug)

        logger.info(
            "assembled_pages_filtered project=%s requested=%d loaded=%d chars=%d",
            project_id, len(page_slugs), len(page_slugs) - len(slug_set), total,
        )
        return "\n".join(parts)

    # ── 전체 모드: 예산 배분으로 모든 페이지 로드 ──
    html_pages = []
    for v in versions:
        content = v["storage_path"] or ""
        if "<!DOCTYPE" in content:
            html_pages.append(content)

    if not html_pages:
        return ""

    parts = [
        "\n\n## ⚠️ 페이지 조립 HTML — 이 HTML을 React 컴포넌트로 1:1 변환 (필수)",
        "",
        "아래는 디자인 토큰 + 컴포넌트 라이브러리 + 페이지 레시피로 조립된 **확정 HTML**입니다.",
        "이 HTML의 디자인(색상, 간격, 폰트, 레이아웃, 그림자, 모서리)을 **정확히 그대로** React/Next.js 컴포넌트로 변환하세요.",
        "",
        "### 변환 규칙",
        "1. HTML의 CSS를 Tailwind/CSS Module/styled-component로 변환하되, **시각적 결과가 동일**해야 함",
        "2. 하드코딩된 CSS 변수(var(--xxx))는 globals.css의 :root에서 디자인 토큰 값 그대로 사용",
        "3. 각 HTML 페이지 = 1개 React 페이지 컴포넌트",
        "4. 반복되는 UI 패턴(헤더, 사이드바, 카드 등)은 공통 컴포넌트로 추출",
        "5. 정적 텍스트/더미 데이터는 그대로 유지 (나중에 API 연동으로 교체)",
        "6. **조립 HTML에 없는 부가 UI(모달, 토스트, 로딩 스피너, 에러 바운더리, 빈 상태 등)는 자유롭게 추가**",
        "7. **조립 HTML에 없는 페이지(404, 설정, 프로필 편집 등)도 프로젝트 맥락에 맞게 추가**",
        "",
    ]

    # 페이지별 HTML 주입 — 스마트 예산 배분 (기존 60K → 12K, 80% 절약)
    FIRST_PAGE_BUDGET = 8000
    SUBSEQUENT_PAGE_BUDGET = 600
    MAX_ASSEMBLED_CHARS = 12000
    total = 0
    for i, html in enumerate(html_pages):
        remaining = MAX_ASSEMBLED_CHARS - total
        if remaining < 300:
            parts.append(f"\n... (나머지 {len(html_pages) - i}개 페이지는 위 패턴에 따라 동일하게 변환)")
            break
        title_match = _re_ap.search(r'<title>(.*?)</title>', html)
        page_title = title_match.group(1) if title_match else f"페이지 {i+1}"
        if i == 0:
            budget = min(FIRST_PAGE_BUDGET, remaining)
            truncated = html[:budget]
            parts.append(f"\n### 페이지 1 (기준 레퍼런스): {page_title}")
            parts.append(f"```html\n{truncated}\n```")
        else:
            budget = min(SUBSEQUENT_PAGE_BUDGET, remaining)
            preview = "\n".join(html.splitlines()[:40])[:budget]
            parts.append(f"\n### 페이지 {i+1}: {page_title} (구조 힌트 — 동일 패턴 적용)")
            parts.append(f"```html\n{preview}\n... (위 패턴으로 React 변환)\n```")
            truncated = preview
        total += len(truncated)

    logger.info("assembled_pages_injected project=%s pages=%d chars=%d",
                project_id, len(html_pages), total)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Workspace path resolution + component path map
# ---------------------------------------------------------------------------

async def _resolve_workspace_path_for_project(
    db: Any,
    project_id: str,
    create: bool = False,
):
    """프로젝트의 workspace 경로 해석.

    Args:
        create: True면 디렉토리가 없어도 경로를 반환 (mkdir은 호출자 책임).
                False면 기존 디렉토리만 반환, 없으면 None.

    Returns:
        Path or None
    """
    from pathlib import Path as _Path

    # 1. DB workspace_deployments에서 기존 경로 조회
    ws_row = await db.fetchone(
        "SELECT workspace_path FROM workspace_deployments WHERE project_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if ws_row and ws_row.get("workspace_path"):
        p = _Path(ws_row["workspace_path"])
        if p.is_dir() or create:
            return p

    # 2. engagement 이름에서 경로 추론
    eng_row = await db.fetchone(
        "SELECT e.name FROM projects p JOIN engagements e ON e.id = p.engagement_id "
        "WHERE p.id=?",
        (project_id,),
    )
    if not eng_row:
        return None

    from engine.workspace.paths import WORKSPACES_ROOT, _make_slug
    slug = _make_slug(eng_row["name"])
    candidate = WORKSPACES_ROOT / slug

    # 3. 기존 디렉토리 정확 매칭
    if candidate.is_dir():
        return candidate

    # 4. 접두사 매칭 (기존 workspace 재사용)
    first_seg = slug.split("-")[0]
    if first_seg and WORKSPACES_ROOT.is_dir():
        for d in WORKSPACES_ROOT.iterdir():
            if d.is_dir() and d.name.startswith(first_seg):
                return d

    # 5. create=True면 새 경로 반환 (아직 디렉토리 없어도)
    if create:
        return candidate

    return None


async def _build_component_path_map(
    db: Any,
    project_id: str,
) -> dict[str, str]:
    """workspace 실제 파일 구조 스캔 → PascalName → import 경로 맵.

    스캔 우선순위:
      1. workspace 디스크 (frontend/src/**/*.tsx)
      2. DB BUILD artifact들의 // FILE: 태그 (공통 인프라 + 컴포넌트 구현)
      3. DB DESIGN 컴포넌트 라이브러리 artifact

    Returns:
        {"Button": "@/components/atoms/Button", "Card": "@/components/common/Card", ...}
    """
    from pathlib import Path as _Path
    import re as _re_cpm

    result: dict[str, str] = {}

    # ── workspace 경로 해석 ──
    ws_path = await _resolve_workspace_path_for_project(db, project_id)

    # ── 1. workspace 디스크 스캔 (가장 정확) ──
    if ws_path:
        fe_src = ws_path / "frontend" / "src"
        if fe_src.is_dir():
            # components/ 뿐 아니라 전체 src/ 에서 .tsx 스캔
            for tsx_file in fe_src.rglob("*.tsx"):
                name = tsx_file.stem
                if name.startswith("_") or name == "index":
                    continue
                # PascalCase인 파일만 (컴포넌트)
                if not name[0].isupper():
                    continue
                rel = tsx_file.relative_to(fe_src)
                import_path = "@/" + str(rel.with_suffix("")).replace("\\", "/")
                result[name] = import_path

    # ── 2. DB artifact에서 // FILE: 태그 스캔 ──
    if not result:
        artifact_rows = await db.fetchall(
            """SELECT av.storage_path AS content FROM nodes n
               JOIN artifacts a ON a.node_id = n.id
               JOIN artifact_versions av ON av.artifact_id = a.id
               JOIN dags d ON d.id = n.dag_id
               WHERE d.project_id = ?
                 AND n.phase = 'BUILD'
                 AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
                 AND av.version_num = a.current_version
               ORDER BY n.name""",
            (project_id,),
        )
        for row in artifact_rows:
            content = row.get("content") or ""
            for m in _re_cpm.finditer(r'// FILE:\s*(src/\S+\.tsx)', content):
                file_path = m.group(1)
                name = file_path.rsplit("/", 1)[-1].replace(".tsx", "")
                if name and name != "index" and not name.startswith("_") and name[0].isupper():
                    import_path = "@/" + file_path.replace(".tsx", "")
                    # 첫 등록 우선 (중복 방지)
                    if name not in result:
                        result[name] = import_path

    # ── 3. DESIGN 컴포넌트 라이브러리 (폴백) ──
    if not result:
        lib_rows = await db.fetchall(
            """SELECT av.storage_path AS content FROM nodes n
               JOIN artifacts a ON a.node_id = n.id
               JOIN artifact_versions av ON av.artifact_id = a.id
               JOIN dags d ON d.id = n.dag_id
               WHERE d.project_id = ?
                 AND (n.name LIKE '%컴포넌트 정의서%'
                      OR n.name = '컴포넌트 라이브러리'
                      OR n.name LIKE '컴포넌트 라이브러리 (%')
                 AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
                 AND av.version_num = a.current_version""",
            (project_id,),
        )
        if lib_rows:
            import re as _re2
            for lib_row in lib_rows:
                content = lib_row.get("content", "")
                if not content:
                    continue
                # JSON 배열인 경우 컴포넌트 name 추출
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list):
                        for comp in parsed:
                            if isinstance(comp, dict) and comp.get("name"):
                                cname = comp["name"]
                                if cname not in result:
                                    result[cname] = f"@/components/{cname}"
                        continue
                except Exception:
                    pass
                # 마크다운 폴백
                for m in _re2.finditer(r'(?:#{2,4})\s*(\w+)(?:\s*컴포넌트)?', content):
                    name = m.group(1).strip()
                    if name and name[0].isupper() and len(name) > 1:
                        result[name] = f"@/components/{name}"

    if result:
        logger.info("component_path_map_built project=%s components=%d", project_id, len(result))
    return result


# ---------------------------------------------------------------------------
# API / DB spec parsers
# ---------------------------------------------------------------------------

async def _load_api_spec_parsed(db: Any, project_id: str) -> dict:
    """API 설계서를 로드하여 엔드포인트 목록으로 파싱.

    Returns: {
        "endpoints": [
            {"method": "GET", "path": "/api/users", "description": "...",
             "response_fields": ["id", "name", "email"]},
            ...
        ],
        "raw": "원본 마크다운 (파싱 실패 시 폴백용)"
    }
    """
    import re as _re

    row = await db.fetchone(
        """SELECT av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.name LIKE '%API%설계%'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version""",
        (project_id,),
    )
    if not row or not row["content"]:
        return {"endpoints": [], "raw": ""}

    raw = row["content"]
    endpoints = []

    # 마크다운 테이블에서 엔드포인트 추출
    # 패턴: | GET | /api/users | 사용자 목록 | ... |
    for m in _re.finditer(
        r'\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*([^\|]+?)\s*\|\s*([^\|]+?)\s*\|',
        raw,
    ):
        method = m.group(1).strip()
        path = m.group(2).strip()
        desc = m.group(3).strip()
        if path.startswith("/"):
            endpoints.append({
                "method": method,
                "path": path,
                "description": desc,
                "response_fields": [],
            })

    # 응답 스키마에서 필드명 추출 (간단 파싱)
    # 패턴: | field_name | string | ... |
    current_endpoint_path = ""
    for line in raw.split("\n"):
        # 엔드포인트 헤딩 감지
        path_match = _re.search(r'(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)', line)
        if path_match:
            current_endpoint_path = path_match.group(2)
            continue

        # 응답 필드 행 감지
        field_match = _re.match(r'\|\s*(\w+)\s*\|\s*(string|number|boolean|integer|array|object)\s*\|', line)
        if field_match and current_endpoint_path:
            field_name = field_match.group(1)
            for ep in endpoints:
                if ep["path"] == current_endpoint_path:
                    ep["response_fields"].append(field_name)
                    break

    return {"endpoints": endpoints, "raw": raw}


async def _load_db_spec_parsed(db: Any, project_id: str) -> dict:
    """DB 설계서를 로드하여 테이블/컬럼 구조로 파싱.

    Returns: {
        "tables": {
            "users": {
                "columns": [
                    {"name": "id", "type": "TEXT", "pk": True},
                    {"name": "email", "type": "TEXT", "nullable": False},
                    ...
                ],
                "description": "사용자 테이블"
            },
            ...
        },
        "raw": "원본 마크다운"
    }
    """
    import re as _re

    row = await db.fetchone(
        """SELECT av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ?
             AND (n.name LIKE '%DB%설계%' OR n.name LIKE '%데이터%모델%')
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version""",
        (project_id,),
    )
    if not row or not row["content"]:
        return {"tables": {}, "raw": ""}

    raw = row["content"]
    tables: dict[str, dict] = {}
    current_table = ""

    for line in raw.split("\n"):
        # 테이블 헤딩 감지: ### users, ## users 테이블, **users** 등
        table_match = re.match(
            r'(?:#{2,4}|[*]{2})\s*(\w+)\s*(?:테이블|table)?',
            line, re.IGNORECASE,
        )
        if table_match:
            current_table = table_match.group(1).lower()
            tables.setdefault(current_table, {"columns": [], "description": ""})
            continue

        # 테이블 설명
        if current_table and line.strip().startswith(">"):
            tables[current_table]["description"] = line.strip().lstrip("> ")
            continue

        # 컬럼 행: | column_name | DATA_TYPE | constraints... |
        if current_table:
            col_match = re.match(
                r'\|\s*(\w+)\s*\|\s*(\w+(?:\(\d+\))?)\s*\|\s*([^\|]*)\s*\|',
                line,
            )
            if col_match:
                col_name = col_match.group(1).strip()
                col_type = col_match.group(2).strip()
                constraints = col_match.group(3).strip().upper()
                # 헤더 행 스킵
                if col_name.lower() in ("컬럼", "column", "name", "필드", "---"):
                    continue
                tables[current_table]["columns"].append({
                    "name": col_name,
                    "type": col_type,
                    "pk": "PK" in constraints or "PRIMARY" in constraints,
                    "nullable": "NOT NULL" not in constraints and "REQUIRED" not in constraints,
                    "fk": "FK" in constraints or "REFERENCES" in constraints,
                })

    return {"tables": tables, "raw": raw}


# ---------------------------------------------------------------------------
# Page count / component names / design tokens for QA
# ---------------------------------------------------------------------------

async def _count_project_pages(db: Any, project_id: str) -> int:
    """프로젝트의 실제 페이지 수 카운트.

    composition_recipes 기준 (정확한 페이지 수).
    레시피 없으면 0 반환 (배치/분할 안 탐).
    주의: artifact_versions HTML 수는 누적이라 부정확.
    """
    try:
        row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM composition_recipes WHERE project_id=?",
            (project_id,),
        )
        return row["c"] if row else 0
    except Exception as exc:
        logger.warning("page_count_query_failed project=%s error=%s", project_id, exc)
        return 0


async def _load_component_names(db: Any, project_id: str) -> list[str]:
    """composition_components 테이블에서 등록된 컴포넌트 이름 목록 조회."""
    try:
        rows = await db.fetchall(
            "SELECT name FROM composition_components WHERE project_id=? ORDER BY name",
            (project_id,),
        )
        return [r["name"] for r in rows]
    except Exception as exc:
        logger.warning("component_names_query_failed project=%s error=%s", project_id, exc)
        return []


async def _load_design_tokens_for_qa(db, node) -> str:
    """QA 디자인 준수 판정용 — 토큰 CSS만 주입 (전체 30K 불필요).

    _validate_design_compliance()가 구체적 위반 항목을 이미 주입했으므로
    QA AI는 토큰 값 참조만 필요 (화면 설계서/UI 시안 불필요).
    """
    rows = await db.fetchall(
        """SELECT av.storage_path AS content
           FROM artifact_versions av
           JOIN artifacts a ON a.id = av.artifact_id
           JOIN nodes n ON n.id = a.node_id
           JOIN dags d ON d.id = n.dag_id
           WHERE d.project_id = ? AND n.phase = 'DESIGN'
             AND n.name LIKE '%디자인 토큰%'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
             AND av.version_num = a.current_version
           LIMIT 1""",
        (node.project_id,),
    )
    if not rows or not rows[0]["content"]:
        return ""
    try:
        tokens = json.loads(rows[0]["content"])
        token_css = _design_tokens_to_css(tokens)
        return (
            "\n\n## 디자인 토큰 참조 (위반 항목 심각도 판정용)\n"
            f"```css\n{token_css}\n```\n"
        )
    except (json.JSONDecodeError, TypeError):
        return ""
