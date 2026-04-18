"""
Artifact savers — DB persistence helpers extracted from executor.py (Phase 3).

Functions that write/update artifacts in the database or workspace filesystem.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core artifact save
# ---------------------------------------------------------------------------

async def _save_artifact(db: Any, node: "NodeSnapshot", content: str, artifact_type: str = "document") -> None:
    """Persist the LLM-generated artifact to the database.

    Args:
        db:      Async DB connection.
        node:    The node that produced the artifact.
        content: Raw content string from the LLM response.
    """
    # JSON 이스케이프 해제 — CLI 프록시가 result를 JSON string escape 상태로 전달할 때
    import json as _json

    # 먼저 content가 이미 유효한 JSON인지 확인 (유효하면 unescape 불필요)
    _already_valid_json = False
    if artifact_type == "json":
        try:
            _json.loads(content)
            _already_valid_json = True
        except (ValueError, _json.JSONDecodeError):
            pass

    if not _already_valid_json and (r'\"' in content or (r'\n' in content and '\\n' not in content[:20])):
        try:
            unescaped = _json.loads(f'"{content}"')
            if artifact_type == "json":
                _json.loads(unescaped)  # 해제 후 유효한 JSON인지 검증
            content = unescaped
        except (ValueError, _json.JSONDecodeError):
            if artifact_type != "json":
                content = (
                    content
                    .replace(r'\n', '\n')
                    .replace(r'\t', '\t')
                    .replace(r'\"', '"')
                    .replace(r"\'", "'")
                    .replace(r'\\', '\\')
                )

    # JSON 산출물: 후처리 정리
    if artifact_type == "json":
        # SELF_CHECK 꼬리 제거
        if "<!-- SELF_CHECK" in content:
            content = content[:content.index("<!-- SELF_CHECK")].rstrip()
        # JSON 유효성 검증 + 자동 수정
        import json as _jfix
        try:
            _jfix.loads(content)
        except (_jfix.JSONDecodeError, ValueError):
            # 리터럴 \\" → " 수정 (이중 이스케이프)
            content = content.replace('\\"', '"')
            # JS 표현식 패턴 수정: "값" + "변수" → "값 {변수}"
            import re as _rfix
            content = _rfix.sub(
                r'"\s*\+\s*"([^"]*?)"\s*\+\s*"',
                lambda m: f' {{{m.group(1)}}} ',
                content,
            )
            # JSON 시작점 탐색: [ (배열) 우선, 없으면 { (객체)
            # CSS의 { 를 JSON 시작으로 오인하는 문제 방지
            start = content.find('[')
            if start == -1:
                start = content.find('{')
            if start > 0:
                content = content[start:]
            # 배열이면 마지막 ], 객체면 마지막 } 까지
            end_char = ']' if content.startswith('[') else '}'
            for i in range(len(content) - 1, -1, -1):
                if content[i] == end_char:
                    content = content[:i + 1]
                    break

    # JSON 유효성 검증 (저장 전 — 깨진 JSON이 current_version이 되는 걸 방지)
    # QA 노드는 제외 (verdict는 자유 형식)
    if artifact_type == "json" and node.node_type != "QA":
        import json as _jsv
        try:
            _jsv.loads(content)
        except _jsv.JSONDecodeError:
            # Extra data 등 정리 시도 (AI가 JSON 뒤에 텍스트 붙이는 경우)
            from engine.skills.executor import _extract_first_json_block
            content = _extract_first_json_block(content)
            try:
                _jsv.loads(content)
                logger.info("json_save_auto_fixed node=%s", node.id[:8])
            except _jsv.JSONDecodeError as e2:
                logger.warning("json_save_rejected node=%s error=%s", node.id[:8], e2)
                raise ValueError(f"JSON 유효성 실패 — 저장 거부: {e2}")

    now = _now()
    import uuid as _uuid
    import hashlib as _hl
    content_hash = _hl.sha256(content.encode("utf-8")).hexdigest()

    # 기존 artifact 있으면 버전 업, 없으면 신규 생성
    existing = await db.fetchone(
        "SELECT id, current_version FROM artifacts WHERE node_id=?", (node.id,)
    )
    if existing:
        artifact_id = existing["id"]
        new_ver = (existing["current_version"] or 0) + 1
        file_type = artifact_type if artifact_type in ("html", "json") else "markdown"
        await db.execute(
            "UPDATE artifacts SET current_version=?, artifact_type=?, file_type=?, updated_at=? WHERE id=?",
            (new_ver, artifact_type, file_type, now, artifact_id),
        )
    else:
        artifact_id = str(_uuid.uuid4())
        new_ver = 1
        file_type = artifact_type if artifact_type in ("html", "json") else "markdown"
        await db.execute(
            "INSERT INTO artifacts (id, node_id, project_id, artifact_type, file_type, "
            "current_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, node.id, node.project_id, artifact_type, file_type, new_ver, now, now),
        )

    # FILE_MANIFEST 블록 파싱 — 산출물에 파일 경로 목록이 포함된 경우 추출
    # 지원 형식 1: FILE_MANIFEST ```json { "files": [{"path": "..."}, ...] } ```
    # 지원 형식 2: <!-- FILE_MANIFEST { "files": [...] } -->
    import re as _re_fm
    file_manifest_json = None
    try:
        fm_match = _re_fm.search(
            r'FILE_MANIFEST\s*```json\s*(\{.*?\})\s*```',
            content,
            _re_fm.DOTALL,
        )
        if not fm_match:
            fm_match = _re_fm.search(
                r'<!--\s*FILE_MANIFEST\s*(\{.*?\})\s*-->',
                content,
                _re_fm.DOTALL,
            )
        if fm_match:
            fm_data = _json.loads(fm_match.group(1))
            file_paths = [f["path"] for f in fm_data.get("files", []) if f.get("path")]
            if file_paths:
                file_manifest_json = _json.dumps(file_paths, ensure_ascii=False)
                logger.info(
                    "file_manifest_parsed node_id=%s files=%d",
                    node.id, len(file_paths),
                )
    except Exception as _fm_exc:
        # FILE_MANIFEST 파싱 실패는 치명적이지 않음 — 기존 방식으로 저장
        logger.debug("file_manifest_parse_error node_id=%s error=%s", node.id, str(_fm_exc))
        file_manifest_json = None

    # artifact_versions에 내용 저장 (file_manifest 포함)
    # 022 마이그레이션 적용 여부와 관계없이 안전하게 동작하도록 fallback 처리:
    #   - file_manifest 컬럼이 있으면 포함하여 INSERT
    #   - 컬럼이 없으면 (마이그레이션 미적용) 기존 방식으로 INSERT
    try:
        await db.execute(
            "INSERT INTO artifact_versions (id, artifact_id, version_num, "
            "storage_path, content_hash, size_bytes, file_manifest, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'agent', ?)",
            (str(_uuid.uuid4()), artifact_id, new_ver, content, content_hash, len(content),
             file_manifest_json, now),
        )
    except Exception as _col_err:
        # file_manifest 컬럼 없음 (마이그레이션 미적용) → 기존 방식으로 재시도
        if "file_manifest" in str(_col_err).lower() or "no column" in str(_col_err).lower():
            logger.debug("file_manifest_column_missing — falling back to legacy INSERT")
            await db.execute(
                "INSERT INTO artifact_versions (id, artifact_id, version_num, "
                "storage_path, content_hash, size_bytes, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'agent', ?)",
                (str(_uuid.uuid4()), artifact_id, new_ver, content, content_hash,
                 len(content), now),
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Chunked HTML snapshot warming (부분 재시도 지원)
# ---------------------------------------------------------------------------

async def warm_chunked_html_snapshot_from_artifact(
    db: Any, node_id: str, artifact_html: str,
) -> tuple[int, list[str]]:
    """현재 artifact HTML 에서 성공 섹션만 추출해 task_snapshot 을 채운다.

    `_chunked_html_items_generate` 는 시작 시 task_snapshot.completed_items 를
    읽어 해당 item 은 LLM 호출 없이 재사용한다. 본 헬퍼는 `data-incomplete="true"`
    placeholder 섹션만 제외하고 나머지 성공 섹션을 캐시에 복원한다. 이후 노드를
    READY 로 전이시키면 엔진은 실패한 item 만 재생성.

    Returns: (복원한 섹션 수, 재시도 대상 id 목록)
    """
    import re as _re
    completed: dict[str, str] = {}
    incomplete: list[str] = []
    # 원형 SC-XX-NNN 패턴을 우선 추출하되 뒤 suffix (예: -dup192, -v2) 는 허용해 매칭한다.
    # 캐시 키 및 섹션 내 id 속성은 원형으로 정규화하여 오염 누적을 차단한다.
    pattern = _re.compile(
        r"<section[^>]*id=['\"](?P<full>SC-[A-Z]{2,5}-\d{3,4}[\w-]*)['\"][^>]*>[\s\S]*?</section>",
    )
    _CANON_ID = _re.compile(r"^(SC-[A-Z]{2,5}-\d{3,4})")
    for m in pattern.finditer(artifact_html):
        section = m.group(0)
        full_id = m.group("full")
        canon_m = _CANON_ID.match(full_id)
        sid = canon_m.group(1) if canon_m else full_id
        if sid != full_id:
            # 오염된 id 속성을 원형으로 치환 (첫 번째 occurrence 만 — outer section 열림 태그)
            section = section.replace(f'id="{full_id}"', f'id="{sid}"', 1)
            section = section.replace(f"id='{full_id}'", f"id='{sid}'", 1)
        if "data-incomplete=\"true\"" in section or "data-incomplete='true'" in section:
            incomplete.append(sid)
            continue
        # 이미 같은 canonical id 로 들어온 섹션이 있으면 마지막 것 유지 (최신 생성 우선)
        completed[sid] = section

    snap = {
        "type": "chunked_html_items",
        "completed_items": completed,
        "completed_count": len(completed),
        "total_count": len(completed) + len(incomplete),
        "updated_at": _now(),
    }
    await db.execute(
        "UPDATE nodes SET task_snapshot=?, updated_at=? WHERE id=?",
        (json.dumps(snap, ensure_ascii=False), _now(), node_id),
    )
    logger.info(
        "chunked_html_snapshot_warmed node=%s restored=%d retry=%d",
        node_id[:8], len(completed), len(incomplete),
    )
    return len(completed), incomplete


# ---------------------------------------------------------------------------
# Scaffold artifact save + workspace write
# ---------------------------------------------------------------------------

async def _save_scaffold_as_artifact(
    db: Any, node: "NodeSnapshot", scaffold_code: dict[str, str],
) -> str:
    """프로그래매틱 골격을 artifact로 직접 저장 (AI 0회).

    Args:
        scaffold_code: {page_slug: "// FILE: ...\\nexport default ..."} 매핑

    Returns:
        병합된 전체 골격 문자열 (// FILE: 구분)
    """
    import uuid as _uuid
    import hashlib as _hl

    import re as _re_scaffold

    # // FILE: 태그에서 파일 경로 추출 → FILE_MANIFEST + file_manifest JSON
    file_paths = _re_scaffold.findall(r"// FILE: (\S+)", "\n".join(scaffold_code.values()))

    merged = "\n\n".join(
        scaffold_code[slug] for slug in sorted(scaffold_code)
    )

    # FILE_MANIFEST 주석 추가 (QA의 _save_artifact 파싱이 잡을 수 있도록)
    if file_paths:
        manifest_json = json.dumps(
            {"files": [{"path": p} for p in file_paths]}, ensure_ascii=False
        )
        merged += f"\n\n<!-- FILE_MANIFEST {manifest_json} -->"

    # file_manifest 컬럼용 JSON 배열
    file_manifest_col = json.dumps(file_paths, ensure_ascii=False) if file_paths else None

    now = _now()
    content_hash = _hl.sha256(merged.encode("utf-8")).hexdigest()

    # artifact 업서트
    existing = await db.fetchone(
        "SELECT id, current_version FROM artifacts WHERE node_id=?", (node.id,)
    )
    if existing:
        artifact_id = existing["id"]
        new_ver = (existing["current_version"] or 0) + 1
        await db.execute(
            "UPDATE artifacts SET current_version=?, artifact_type='code', "
            "file_type='code', updated_at=? WHERE id=?",
            (new_ver, now, artifact_id),
        )
    else:
        artifact_id = str(_uuid.uuid4())
        new_ver = 1
        await db.execute(
            "INSERT INTO artifacts (id, node_id, project_id, artifact_type, file_type, "
            "current_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'code', 'code', ?, ?, ?)",
            (artifact_id, node.id, node.project_id, new_ver, now, now),
        )

    try:
        await db.execute(
            "INSERT INTO artifact_versions (id, artifact_id, version_num, "
            "storage_path, content_hash, size_bytes, file_manifest, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'scaffold', ?)",
            (str(_uuid.uuid4()), artifact_id, new_ver, merged, content_hash,
             len(merged), file_manifest_col, now),
        )
    except Exception as exc:
        logger.warning("scaffold_artifact_save_fallback file_manifest column missing: %s", exc)
        await db.execute(
            "INSERT INTO artifact_versions (id, artifact_id, version_num, "
            "storage_path, content_hash, size_bytes, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'scaffold', ?)",
            (str(_uuid.uuid4()), artifact_id, new_ver, merged, content_hash,
             len(merged), now),
        )

    logger.info(
        "scaffold_artifact_saved node=%s pages=%d size=%d ver=%d",
        node.id[:8], len(scaffold_code), len(merged), new_ver,
    )

    # ── workspace에도 직접 파일 쓰기 (auto_deploy가 DB에서 재파싱 안 해도 되도록) ──
    try:
        await _write_scaffold_to_workspace(db, node.project_id, merged)
    except Exception as exc:
        import traceback
        logger.warning(
            "scaffold_workspace_write_failed error=%s\n%s",
            str(exc), traceback.format_exc(),
        )

    return merged


async def _write_scaffold_to_workspace(
    db: Any,
    project_id: str,
    merged_content: str,
) -> None:
    """프로그래매틱 코드를 workspace 디렉토리에 직접 파일로 쓰기."""
    import re as _re_ws
    from engine.skills.artifact.loader import (
        _resolve_workspace_path_for_project,
        _build_component_path_map,
    )

    # ── 1. workspace 경로 해석 (create=True: 디렉토리 없어도 경로 반환) ──
    workspace_path = await _resolve_workspace_path_for_project(db, project_id, create=True)
    if not workspace_path:
        logger.warning("scaffold_ws_skip project=%s — 경로 해석 불가 (engagement 없음?)", project_id)
        return

    workspace_path.mkdir(parents=True, exist_ok=True)
    logger.info("scaffold_ws_target project=%s path=%s content_len=%d", project_id, workspace_path, len(merged_content))

    # ── 2. // FILE: 태그 존재 확인 ──
    file_tag_count = merged_content.count("// FILE:")
    if file_tag_count == 0:
        logger.warning("scaffold_ws_no_file_tags project=%s — merged_content에 // FILE: 없음", project_id)
        return

    # ── 3. component_path_map 구축 (import 보정용, 실패해도 진행) ──
    cpm: dict[str, str] = {}
    try:
        cpm = await _build_component_path_map(db, project_id)
    except Exception as exc:
        logger.debug("scaffold_ws_cpm_failed error=%s", str(exc))

    # ── 4. // FILE: 태그 기반 분할 + 파일 쓰기 ──
    from engine.workspace.paths import _resolve_workspace_path, _sanitize_code_for_workspace
    parts = _re_ws.split(r'^// FILE:\s*(\S+)', merged_content, flags=_re_ws.MULTILINE)

    logger.info("scaffold_ws_split project=%s parts=%d file_tags=%d", project_id, len(parts), file_tag_count)

    written = 0
    skipped = 0
    i = 1
    while i < len(parts) - 1:
        filepath = parts[i].strip()
        raw_code = parts[i + 1]
        i += 2
        if not filepath:
            skipped += 1
            continue
        if not raw_code or not raw_code.strip():
            skipped += 1
            continue

        try:
            code = _sanitize_code_for_workspace(raw_code.strip(), filepath, component_path_map=cpm)
        except Exception:
            code = raw_code.strip()  # sanitizer 실패해도 원본으로 진행

        if not code:
            skipped += 1
            continue

        target = _resolve_workspace_path(filepath, workspace_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        written += 1

    logger.info(
        "scaffold_ws_result project=%s written=%d skipped=%d path=%s",
        project_id, written, skipped, workspace_path,
    )

    # ── 5. 빌드 검증 (파일이 있을 때만) ──
    # 검증 실패를 ERROR 수준으로 승격. 파일은 이미 쓰여 있어야 next build가
    # 가능하므로 "완전한 롤백"은 비현실적이나, 실패 사실을 호출부에 전파하여
    # auto_deploy 단계에서 배포 차단 판단이 가능하도록 예외를 raise.
    # (호출부 _write_scaffold_to_workspace 호출은 이미 try/except로 감싸져 있어
    # 전체 저장 흐름은 깨지지 않고, 로그가 명확히 FAIL로 남는다.)
    if written > 0:
        try:
            from engine.workspace.programmatic_verify import programmatic_build_verify
            verify_result = programmatic_build_verify(workspace_path, component_path_map=cpm)
        except Exception as exc:
            logger.warning("scaffold_build_verify_skipped error=%s", str(exc))
            verify_result = None

        if verify_result is not None:
            if verify_result.get("success"):
                logger.info(
                    "scaffold_build_verify_pass project=%s attempts=%s fixes=%d",
                    project_id,
                    verify_result.get("attempts", 0),
                    len(verify_result.get("fixes", [])),
                )
            elif not verify_result.get("skipped"):
                err_snippet = verify_result.get("errors", ["?"])[:1]
                logger.error(
                    "scaffold_build_verify_fail project=%s errors=%s fixes=%d",
                    project_id, err_snippet,
                    len(verify_result.get("fixes", [])),
                )
                # 배포 단계에서 실패를 인지할 수 있도록 마커 파일 기록.
                # (DB 스키마 변경 없이 아티팩트 경로에 플래그만 남김 — 후속
                # auto_deploy 단계에서 이 파일 존재 시 SUCCESS 마킹 차단.)
                try:
                    marker = workspace_path / ".build_verify_failed"
                    marker.write_text(
                        str(err_snippet)[:500], encoding="utf-8"
                    )
                except Exception:
                    pass
                # 상위로 전파 (호출부 try/except에서 잡혀 저장 흐름은 유지).
                raise RuntimeError(
                    f"programmatic_build_verify failed: {err_snippet}"
                )


# ---------------------------------------------------------------------------
# AI stub extraction / merge
# ---------------------------------------------------------------------------

def _extract_ai_stubs(scaffold_code: dict[str, str]) -> str:
    """골격에서 AI가 채워야 할 빈 함수/타입 stub만 추출하여 경량 프롬프트 생성.

    추출 대상:
      - interface ...Data { ... }  (TypeScript 인터페이스)
      - const fetchData = ...      (데이터 패칭 훅)
      - const handle* = ...        (이벤트 핸들러)
      - /* AI: ... */ 주석 블록

    Returns:
        AI에게 전달할 경량 프롬프트 문자열 (~3-5K)
    """
    import re as _re

    parts = [
        "## 🔧 구현 요청: 아래 빈 함수/타입만 채워서 출력하세요\n",
        "골격 코드는 이미 저장되어 있습니다. **전체 페이지를 재출력하지 마세요.**\n"
        "아래 각 파일의 빈 구현체만 완성하여 출력하세요.\n\n"
        "### 출력 형식 (필수)\n"
        "```\n"
        "// IMPL: src/pages/SomePagePage.tsx\n"
        "\n"
        "interface SomePageData {\n"
        "  // 실제 타입 정의\n"
        "}\n"
        "\n"
        "const fetchData = useCallback(async () => {\n"
        "  // 실제 API 호출\n"
        "}, []);\n"
        "\n"
        "const handleAction = useCallback(async (...args: any[]) => {\n"
        "  // 실제 핸들러\n"
        "}, []);\n"
        "```\n\n"
        "**규칙:**\n"
        "- `// IMPL: {파일경로}` 태그로 파일 구분\n"
        "- interface, fetchData, handle* 함수 본문만 출력\n"
        "- import/export/JSX/컴포넌트 배치는 출력하지 마세요 (이미 골격에 있음)\n"
        "- 모달 콘텐츠가 필요하면 `// MODAL_CONTENT:` 블록으로 출력\n\n",
    ]

    for slug in sorted(scaffold_code):
        code = scaffold_code[slug]
        lines = code.split("\n")

        # 파일 경로 추출
        file_path = ""
        for line in lines:
            if line.startswith("// FILE:"):
                file_path = line.replace("// FILE:", "").strip()
                break

        # stub 영역 추출
        stubs = []
        in_stub = False
        brace_depth = 0
        stub_lines = []

        for line in lines:
            stripped = line.strip()

            # interface 블록
            if stripped.startswith("interface ") and "{" in stripped:
                in_stub = True
                brace_depth = 0
                stub_lines = [line]
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth == 0:
                    stubs.append("\n".join(stub_lines))
                    in_stub = False
                continue

            # fetchData, handle* 함수
            if (
                not in_stub
                and ("const fetchData" in stripped or "const handle" in stripped)
                and "useCallback" in stripped
            ):
                in_stub = True
                brace_depth = 0
                stub_lines = [line]
                brace_depth += stripped.count("{") - stripped.count("}")
                brace_depth += stripped.count("(") - stripped.count(")")
                if brace_depth <= 0 and stripped.endswith(";"):
                    stubs.append("\n".join(stub_lines))
                    in_stub = False
                continue

            # /* AI: ... */ 단일 라인
            if not in_stub and "/* AI:" in stripped:
                stubs.append(f"  // 위치: {line.strip()}")
                continue

            # 모달 콘텐츠 마커
            if not in_stub and "{/* AI: 모달 콘텐츠 구현 */}" in stripped:
                stubs.append("  // MODAL_CONTENT: 모달 내부 JSX 필요")
                continue

            # 진행 중인 stub 수집
            if in_stub:
                stub_lines.append(line)
                brace_depth += stripped.count("{") - stripped.count("}")
                brace_depth += stripped.count("(") - stripped.count(")")
                # 함수 끝 감지: depth 0 + 세미콜론
                if brace_depth <= 0 and (stripped.endswith(";") or stripped.endswith("};")):
                    stubs.append("\n".join(stub_lines))
                    in_stub = False

        if stubs:
            parts.append(f"### `{file_path or slug}`\n")
            parts.append("```tsx")
            parts.append(f"// IMPL: {file_path}")
            parts.append("")
            parts.append("\n\n".join(stubs))
            parts.append("```\n")

    return "\n".join(parts)


def _merge_ai_into_scaffold(
    scaffold_code: dict[str, str],
    ai_output: str,
) -> str:
    """AI가 반환한 구현체를 골격 코드에 프로그래매틱 삽입.

    AI 출력 형식 (우선순위):
      1. // IMPL: src/pages/SomePage.tsx  (지시 형식)
      2. // FILE: src/pages/SomePage.tsx  (폴백: AI가 전체 출력한 경우)
      3. 태그 없음 (폴백: 단일 파일 impl로 간주)

    삽입 규칙:
      - interface → 기존 interface 블록 교체
      - fetchData → 기존 fetchData 블록 교체
      - handle* → 기존 handle* 블록 교체
      - MODAL_CONTENT → 모달 콘텐츠 주석 위치에 삽입

    AI가 전체 페이지를 재출력한 경우:
      - // FILE: 태그 + export default 감지 → 골격 대신 AI 출력 사용 (폴백)

    Returns:
        병합된 전체 코드 (// FILE: 구분)
    """
    import re as _re
    from engine.skills.utils import _extract_block, _extract_all_blocks, _extract_section

    # AI 출력을 IMPL/FILE 블록별로 파싱
    impl_blocks: dict[str, str] = {}
    file_blocks: dict[str, str] = {}  # AI가 전체 출력한 경우
    current_file = None
    current_tag = None  # "impl" or "file"
    current_lines: list[str] = []

    for line in ai_output.split("\n"):
        stripped = line.strip()

        # 코드 펜스 무시
        if stripped.startswith("```"):
            continue

        if stripped.startswith("// IMPL:"):
            if current_file and current_lines:
                target = impl_blocks if current_tag == "impl" else file_blocks
                target[current_file] = "\n".join(current_lines)
            current_file = stripped.replace("// IMPL:", "").strip()
            current_tag = "impl"
            current_lines = []
        elif stripped.startswith("// FILE:"):
            if current_file and current_lines:
                target = impl_blocks if current_tag == "impl" else file_blocks
                target[current_file] = "\n".join(current_lines)
            current_file = stripped.replace("// FILE:", "").strip()
            current_tag = "file"
            current_lines = [line]  # // FILE: 라인 포함
        elif current_file is not None:
            current_lines.append(line)

    if current_file and current_lines:
        target = impl_blocks if current_tag == "impl" else file_blocks
        target[current_file] = "\n".join(current_lines)

    # 폴백: 태그가 하나도 없으면 전체 출력을 단일 impl로 간주
    if not impl_blocks and not file_blocks:
        # fetchData나 handle이 있으면 구현체로 취급
        if "fetchData" in ai_output or "handle" in ai_output:
            slugs = sorted(scaffold_code.keys())
            if slugs:
                # 첫 번째 scaffold의 파일 경로 추출
                first_code = scaffold_code[slugs[0]]
                fp = ""
                for cl in first_code.split("\n"):
                    if cl.startswith("// FILE:"):
                        fp = cl.replace("// FILE:", "").strip()
                        break
                impl_blocks[fp or slugs[0]] = ai_output
                logger.warning(
                    "merge_fallback_no_tags — treating entire AI output as single impl"
                )

    # AI가 // FILE:로 전체 페이지를 출력한 경우 → 골격 대신 AI 출력 사용 (폴백)
    if not impl_blocks and file_blocks:
        _has_full_pages = any(
            "export default" in content for content in file_blocks.values()
        )
        if _has_full_pages:
            logger.warning(
                "merge_fallback_full_output — AI output has full pages (%d files), "
                "using AI output instead of scaffold merge",
                len(file_blocks),
            )
            return "\n\n".join(
                content for content in file_blocks.values()
            )

    # 각 scaffold 파일에 구현체 삽입
    merged_files: list[str] = []

    for slug in sorted(scaffold_code):
        code = scaffold_code[slug]

        # 매칭할 IMPL 파일 경로 찾기
        file_path = ""
        for codeline in code.split("\n"):
            if codeline.startswith("// FILE:"):
                file_path = codeline.replace("// FILE:", "").strip()
                break

        impl = impl_blocks.get(file_path, "")
        if not impl:
            # slug 기반 폴백 매칭
            for impl_path, impl_code in impl_blocks.items():
                if slug.replace("-", "") in impl_path.lower().replace("-", ""):
                    impl = impl_code
                    break

        if not impl:
            merged_files.append(code)
            continue

        # 구현체에서 개별 블록 추출
        impl_interface = _extract_block(impl, r"interface\s+\w+")
        impl_fetch = _extract_block(impl, r"const\s+fetchData\s*=")
        impl_handlers = _extract_all_blocks(impl, r"const\s+handle\w+\s*=")
        impl_modal = _extract_section(impl, "// MODAL_CONTENT:")

        result_lines = []
        in_replace = False
        replace_type = ""
        brace_depth = 0

        for line in code.split("\n"):
            stripped = line.strip()

            # interface 교체
            if impl_interface and stripped.startswith("interface ") and "{" in stripped:
                result_lines.append(impl_interface)
                in_replace = True
                replace_type = "interface"
                brace_depth = stripped.count("{") - stripped.count("}")
                if brace_depth == 0:
                    in_replace = False
                continue

            # fetchData 교체
            if impl_fetch and "const fetchData" in stripped and "useCallback" in stripped:
                result_lines.append(impl_fetch)
                in_replace = True
                replace_type = "fetch"
                brace_depth = 1
                continue

            # handle* 교체
            if impl_handlers and stripped.startswith("const handle") and "useCallback" in stripped:
                fn_match = _re.match(r"const\s+(handle\w+)", stripped)
                fn_name = fn_match.group(1) if fn_match else ""
                replacement = None
                for h in impl_handlers:
                    if fn_name and fn_name in h:
                        replacement = h
                        break
                if replacement is None and impl_handlers:
                    replacement = impl_handlers.pop(0)
                if replacement:
                    result_lines.append(replacement)
                    in_replace = True
                    replace_type = "handler"
                    brace_depth = 1
                    continue

            # 모달 콘텐츠 교체
            if impl_modal and "{/* AI: 모달 콘텐츠 구현 */}" in stripped:
                result_lines.append(impl_modal)
                continue

            # skip 중인 블록
            if in_replace:
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0 and (stripped.endswith(";") or stripped.endswith("}") or stripped == ""):
                    in_replace = False
                continue

            result_lines.append(line)

        merged_files.append("\n".join(result_lines))

    return "\n\n".join(merged_files)
