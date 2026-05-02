"""
Executor Cascade helpers — downstream cascade classification and triggering.

Extracted from executor.py for maintainability.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


# 거시 진단 도입 모드: dry-run | keyword-only | full
# - dry-run: 로그/audit 만 기록, INVALID 전환 안 함 (안전 점진 도입용)
# - keyword-only: 키워드 매칭만, AI fallback 비활성 (안전 default)
# - full: 키워드 + AI fallback 모두 활성 (운영 안정화 후 권장)
#
# Default 가 'keyword-only' 인 이유: 키워드는 strict 매칭으로 false positive 거의 없고
# 비용 0. AI fallback 은 응답 형식/지연 변동 위험이 있어 명시 opt-in.
def _rework_mode() -> str:
    return (os.environ.get("V10_UPSTREAM_REWORK_MODE", "keyword-only") or "keyword-only").strip().lower()


# 킬 스위치: V10_UPSTREAM_REWORK_KILL=1 이면 거시 진단 즉시 비활성
# 운영 중 문제 발생 시 환경변수 변경만으로 즉시 차단 가능 (서버 재시작 불필요는
# 프로세스 환경변수 갱신 가능 시. 일반적으로 재시작 1회 필요).
def _is_killed() -> bool:
    return (os.environ.get("V10_UPSTREAM_REWORK_KILL", "") or "").strip() in ("1", "true", "yes")


async def _cascade_for_node(
    db: Any,
    source_node_id: str,
    ds_node: dict,
    diff: str,
    old_content: str,
    model_adapter: Any,
) -> None:
    """
    단일 직접 downstream 노드에 대한 cascade 분류 + INVALID 전이.

    직접 downstream만 처리 — deep BFS 없음.
    각 노드가 재실행 완료 시 _trigger_downstream_cascade가 다시 호출되어
    결과가 달라진 경우에만 다음 단계로 자연 전파.
    결과가 동일하면 generate_diff → 빈 diff → 전파 중단.
    """
    from engine.core.cascade import CascadeInvalidator
    from engine.ai.change_classifier import classify_change

    classification = await classify_change(
        diff=diff,
        downstream_node_name=ds_node["name"],
        downstream_artifact_type=ds_node["node_type"],
        model_adapter=model_adapter,
        old_content=old_content,
    )
    change_type = classification["type"]
    affected_sections = classification.get("affected_sections", [])

    # 직접 downstream 노드만 마킹 + INVALID 전이 (deep BFS 제거)
    now = _now()
    # CONTEXTUAL은 전체 재실행이므로 섹션 정보 불필요 → NULL로 저장
    sections_json = json.dumps(affected_sections, ensure_ascii=False) if change_type == "PARTIAL" else None
    await db.execute(
        """UPDATE nodes
           SET invalidation_pending=1, invalidation_source_id=?,
               invalidation_queued_at=?, invalidation_change_type=?,
               invalidation_affected_sections=?, updated_at=?
           WHERE id=? AND invalidation_pending=0""",
        (source_node_id, now, change_type, sections_json, now, ds_node["id"]),
    )

    cascade = CascadeInvalidator(db)
    await cascade.phase2_apply_invalid(ds_node["id"])

    # 버그 2: cascade INVALID 시 retry_count 리셋 (이전 실패 횟수가 재실행을 막지 않도록)
    await db.execute(
        "UPDATE nodes SET retry_count=0 WHERE id=? AND state='INVALID'",
        (ds_node["id"],),
    )

    # Chunked items partial patch — downstream 이 chunked html/json 스킬이고
    # task_snapshot.failed_items_last_attempt 기록이 있으면, atomic_state 에서
    # 해당 item 만 FAILED 로 전이해 다음 실행 시 cache bust 되도록 한다.
    # 전체 DELETE 하지 않음 — 나머지 item 캐시는 보존 (토큰 절감).
    # feature flag 기본 off — 명시적 on 시에만 작동.
    if os.environ.get("V10_CHUNKED_ITEMS_PARTIAL_RETRY", "0") == "1":
        try:
            _snap_row = await db.fetchone(
                "SELECT task_snapshot FROM nodes WHERE id=?", (ds_node["id"],),
            )
            _failed_items: list[str] = []
            if _snap_row and _snap_row.get("task_snapshot"):
                try:
                    _snap = json.loads(_snap_row["task_snapshot"]) or {}
                    if isinstance(_snap, dict):
                        _snap_type = _snap.get("type")
                        if _snap_type in ("chunked_html_items", "chunked_json_items"):
                            _raw = _snap.get("failed_items_last_attempt") or []
                            _failed_items = [
                                str(k) for k in _raw if isinstance(k, str)
                            ]
                except Exception:
                    _failed_items = []
            if _failed_items:
                _placeholders = ",".join(["?"] * len(_failed_items))
                await db.execute(
                    f"""UPDATE atomic_state
                        SET status='FAILED',
                            retry_count=COALESCE(retry_count, 0) + 1,
                            reason='cascade_partial_retry',
                            updated_at=?
                        WHERE node_id=? AND item_key IN ({_placeholders})""",
                    [now, ds_node["id"], *_failed_items],
                )
                logger.info(
                    "cascade_chunked_partial_mark node=%s count=%d keys=%s",
                    ds_node["id"][:8], len(_failed_items),
                    ",".join(_failed_items[:5]),
                )
        except Exception as _cascade_ci_err:
            logger.warning(
                "cascade_chunked_partial_failed node=%s error=%s",
                ds_node["id"][:8], _cascade_ci_err,
            )

    # 버그 3: 같은 phase의 COMPLETED GATE 리셋 (GATE가 고아 COMPLETED로 남으면 후행 단계가 진행됨)
    await db.execute(
        """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, updated_at=?
           WHERE dag_id=(SELECT dag_id FROM nodes WHERE id=?)
           AND node_type='GATE' AND state='COMPLETED'
           AND phase=(SELECT phase FROM nodes WHERE id=?)""",
        (now, ds_node["id"], ds_node["id"]),
    )

    # QA pair 노드 리셋: TASK가 INVALID되면 이전 QA 결과도 무효
    await db.execute(
        """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, retry_count=0, updated_at=?
           WHERE (task_pair_node_id=? OR qa_pair_node_id=?)
           AND node_type='QA' AND state IN ('COMPLETED', 'BLOCKED')""",
        (now, ds_node["id"], ds_node["id"]),
    )

    logger.info(
        "downstream_cascade_node node=%s ds=%s type=%s",
        source_node_id[:8], ds_node["id"][:8], change_type,
    )

    # cascade 후 advancer에 DAG enqueue — INVALID 노드가 즉시 픽업되도록
    try:
        dag_row = await db.fetchone(
            "SELECT dag_id FROM nodes WHERE id=?", (ds_node["id"],),
        )
        if dag_row:
            from api.server import _dag_advancer
            if _dag_advancer:
                await _dag_advancer.enqueue(dag_row["dag_id"])
    except Exception:
        pass  # advancer 없어도 다음 주기에 자연 픽업됨


async def _trigger_downstream_cascade(
    db: Any,
    node: "NodeSnapshot",
    model_adapter: "ModelAdapter",
) -> None:
    """
    노드 완료 후 artifact 변경 여부 체크 → 변경 시 downstream cascade 트리거.
    이전 버전이 없으면 (첫 실행) cascade 없음.

    수정 이유:
      - storage_path는 파일 경로가 아니라 콘텐츠 자체 (DB 직접 사용)
      - downstream 노드별 개별 분류 (단일 대표 분류 → 오분류 제거)
      - asyncio.create_task로 백그라운드 실행 (동기 블로킹 제거)
    """
    from engine.ai.change_classifier import generate_diff

    # 이전 버전 artifact 존재 여부 확인 (version_num >= 2 이면 재실행)
    # storage_path 컬럼 = 콘텐츠 자체 (파일 경로 아님)
    versions = await db.fetchall(
        """SELECT av.version_num, av.storage_path AS content
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE a.node_id=?
           ORDER BY av.version_num DESC
           LIMIT 2""",
        (node.id,),
    )
    if len(versions) < 2:
        # 첫 실행이지만, downstream 중 이미 COMPLETED인 노드가
        # 이 노드보다 먼저 완료됐다면 INVALID 처리 (선행 없이 완료된 케이스)
        await _invalidate_early_completed_downstreams(db, node)
        return

    old_content: str = versions[1]["content"] or ""
    new_content: str = versions[0]["content"] or ""

    if not old_content or not new_content:
        return

    diff = generate_diff(old_content, new_content)
    if not diff:
        return  # 내용 동일 — cascade 불필요

    # ── diff GATE (3-A): 공백/포매팅 전용 변경 필터 ──
    # 실질 내용이 없는 변경(공백·빈 줄만)은 cascade 불필요
    _meaningful_changed = [
        ln for ln in diff.split("\n")
        if ln and ln[0] in ("+", "-")
        and not ln.startswith("+++") and not ln.startswith("---")
        and ln[1:].strip()   # 공백·탭만 있는 줄 제외
    ]
    if not _meaningful_changed:
        logger.debug("cascade_gate_skip node=%s reason=whitespace_only", node.id[:8])
        return

    # 직접 연결된 downstream 노드 전체 조회 (한계 1 수정 — 대표 1개 → 전체 개별 분류)
    downstream_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.node_type
           FROM edges e
           JOIN nodes n ON n.id = e.to_node_id
           WHERE e.from_node_id=? AND e.is_active=1""",
        (node.id,),
    )
    if not downstream_nodes:
        return

    async def _run_all():
        for ds in downstream_nodes:
            try:
                await _cascade_for_node(db, node.id, ds, diff, old_content, model_adapter)
            except Exception as _e:
                logger.warning(
                    "cascade_per_node_failed node=%s ds=%s error=%s",
                    node.id[:8], ds["id"][:8], _e,
                )

    task = asyncio.create_task(_run_all(), name=f"cascade-{node.id[:8]}")
    task.add_done_callback(_log_task_exception)


def _log_task_exception(task: asyncio.Task) -> None:
    """Background task의 미처리 예외를 로깅."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background_task_failed name=%s error=%s", task.get_name(), exc)


async def _trigger_upstream_cascade(
    db: Any,
    node: "NodeSnapshot",
    model_adapter: "ModelAdapter",
) -> None:
    """노드 완료 후 상위(upstream) COMPLETED 노드에 cascade 전파.

    하위가 변경되면 상위 정의/명세도 갱신 필요할 수 있음.
    예: 컴포넌트 라이브러리에 modal 추가 → 컴포넌트 정의서에도 반영.

    무한 루프 방지:
    - invalidation_source_id 체인 추적 — 원인 노드는 스킵
    - INVALID 상태 노드 스킵 (이미 cascade 처리 중)
    """
    from engine.ai.change_classifier import generate_diff

    # 이전 버전 존재해야 diff 가능
    versions = await db.fetchall(
        """SELECT av.version_num, av.storage_path AS content
           FROM artifacts a
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE a.node_id=?
           ORDER BY av.version_num DESC
           LIMIT 2""",
        (node.id,),
    )
    if len(versions) < 2:
        return

    old_content: str = versions[1]["content"] or ""
    new_content: str = versions[0]["content"] or ""
    if not old_content or not new_content:
        return

    diff = generate_diff(old_content, new_content)
    if not diff:
        return

    # 의미 있는 변경인지 체크
    _meaningful = [
        ln for ln in diff.split("\n")
        if ln and ln[0] in ("+", "-")
        and not ln.startswith("+++") and not ln.startswith("---")
        and ln[1:].strip()
    ]
    if not _meaningful:
        return

    # 직접 상위(upstream) 노드 조회: edges에서 이 노드가 to_node_id인 것
    upstream_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.node_type, n.state, n.invalidation_source_id
           FROM edges e
           JOIN nodes n ON n.id = e.from_node_id
           WHERE e.to_node_id=? AND e.is_active=1
             AND n.node_type='TASK'""",
        (node.id,),
    )
    if not upstream_nodes:
        return

    # 이 노드의 원인 노드 (루프 방지용)
    my_row = await db.fetchone(
        "SELECT invalidation_source_id FROM nodes WHERE id=?", (node.id,),
    )
    my_source = my_row["invalidation_source_id"] if my_row else None

    for us in upstream_nodes:
        # COMPLETED만 대상
        if us["state"] != "COMPLETED":
            continue
        # 원인 노드면 스킵 (루프 방지)
        if us["id"] == my_source:
            logger.debug("upstream_cascade_skip_source node=%s us=%s", node.id[:8], us["id"][:8])
            continue
        # GATE/QA 노드 제외
        if us["node_type"] != "TASK":
            continue

        try:
            await _cascade_for_node(db, node.id, us, diff, old_content, model_adapter)
            logger.info(
                "upstream_cascade_node node=%s upstream=%s name=%s",
                node.id[:8], us["id"][:8], us["name"],
            )
        except Exception as _e:
            logger.warning(
                "upstream_cascade_failed node=%s us=%s error=%s",
                node.id[:8], us["id"][:8], _e,
            )


async def _invalidate_early_completed_downstreams(
    db: Any, node: "NodeSnapshot",
) -> None:
    """첫 실행 완료된 노드의 downstream 중 먼저 COMPLETED된 노드를 INVALID 처리.

    선행 노드 없이 실행 완료된 하위 노드를 감지하여 재실행 트리거.
    - downstream.state == 'COMPLETED' AND completed_at < now
    - is_active=1 edge로 직접 연결된 노드만 대상
    - QA 쌍 리셋 + 같은 phase GATE 리셋 포함
    """
    now = _now()

    early_completed = await db.fetchall(
        """SELECT n.id, n.name, n.node_type, n.phase, n.dag_id
           FROM edges e
           JOIN nodes n ON n.id = e.to_node_id
           WHERE e.from_node_id = ? AND e.is_active = 1
             AND n.state = 'COMPLETED'
             AND n.completed_at < ?""",
        (node.id, now),
    )
    if not early_completed:
        return

    for ds in early_completed:
        # INVALID 처리
        await db.execute(
            """UPDATE nodes SET state='INVALID',
               invalidation_source_id=?, invalidation_change_type='CONTEXTUAL',
               retry_count=0, updated_at=?
               WHERE id=? AND state='COMPLETED'""",
            (node.id, now, ds["id"]),
        )

        # QA 쌍 리셋
        await db.execute(
            """UPDATE nodes SET state='NOT_STARTED', updated_at=?
               WHERE (task_pair_node_id=? OR qa_pair_node_id=?)
                 AND state NOT IN ('SKIPPED')""",
            (now, ds["id"], ds["id"]),
        )

        # 같은 phase GATE 리셋
        await db.execute(
            """UPDATE nodes SET state='NOT_STARTED', completed_at=NULL, updated_at=?
               WHERE dag_id=? AND node_type='GATE' AND state='COMPLETED'
                 AND phase=?""",
            (now, ds["dag_id"], ds["phase"]),
        )

        logger.info(
            "early_completed_invalidated upstream=%s downstream=%s name=%s",
            node.id[:8], ds["id"][:8], ds["name"],
        )


# ────────────────────────────────────────────────────────────────────────
# S2-5: 역방향 Rework — downstream QA FAIL 사유에 upstream artifact 참조가
# 명시되면 upstream TASK 노드를 INVALID 로 전환하여 root cause 수정 강제.
# 무한 루프 방지: upstream_rework_count 컬럼 조건부 ALTER + phase 당 2회 상한.
# ────────────────────────────────────────────────────────────────────────


# upstream artifact 키워드 (downstream QA verdict 텍스트에서 검출).
# 7개 카테고리로 확장 (범용성 확보). 키워드 매칭은 strict (word boundary 적용).
_UPSTREAM_KEYWORDS: dict[str, list[str]] = {
    "DESIGN": [
        "디자인 시안", "디자인 토큰", "ui 디자인", "디자인 컴포넌트",
        "design token", "design system", "스타일 가이드",
        "디자인 시스템", "tokens",
    ],
    "API": [
        "api 설계", "api 명세", "엔드포인트", "endpoint",
        "request schema", "response schema",
        "rest api", "graphql", "rpc", "webhook",
    ],
    "DB": [
        "db 설계", "스키마", "테이블 정의", "schema definition",
        "정규화", "외래키", "foreign key",
        "마이그레이션", "migration", "엔티티",
    ],
    "REQ": [
        "요구사항", "기능 백로그", "유스케이스",
        "requirement", "user story",
        "기능 명세", "스펙", "spec",
    ],
    "INFRA": [
        "배포", "deployment", "ci/cd", "쿠버네티스", "docker",
        "kubernetes", "infra",
    ],
    "MOBILE": [
        "네이티브", "ios", "android", "react native", "expo",
    ],
    "DATA": [
        "피처", "feature", "모델", "dataset", "파이프라인",
        "pipeline", "ml model",
    ],
}

UPSTREAM_REWORK_LIMIT_PER_PHASE = 2

# AI fallback confidence 임계 — 미만이면 폐기 (false positive 방지)
AI_CONFIDENCE_THRESHOLD = 0.7

# AI fallback 분류 가능 카테고리 화이트리스트
_VALID_CATEGORIES: set[str] = {"DESIGN", "API", "DB", "REQ", "INFRA", "MOBILE", "DATA"}


def _kw_match(text: str, kw: str) -> bool:
    """Strict 키워드 매칭.
    - 영어/숫자/공백/심볼만으로 구성된 키워드: word boundary (\\b) 사용
    - 한국어 포함: 키워드 뒤에 한글/공백/문장부호/문장끝만 허용 (조사 무관 매칭)
    """
    if not kw or not text:
        return False
    kw_lower = kw.lower()
    text_lower = text.lower()
    is_ascii = all(ord(c) < 128 for c in kw_lower)
    if is_ascii:
        return bool(re.search(rf"\b{re.escape(kw_lower)}\b", text_lower))
    # 한국어/혼용: 키워드 뒤가 한글이면 별도 단어 (false positive 차단)
    return bool(
        re.search(rf"{re.escape(kw_lower)}(?![a-z0-9_])", text_lower)
    )


async def _ensure_rework_count_column(db: Any) -> None:
    """nodes.upstream_rework_count 컬럼이 없으면 ALTER. idempotent."""
    try:
        await db.execute(
            "ALTER TABLE nodes ADD COLUMN upstream_rework_count INTEGER DEFAULT 0"
        )
    except Exception:
        # 이미 존재 → 무시
        pass


def _classify_upstream_categories(verdict_text: str) -> set[str]:
    """QA verdict 텍스트에서 어느 upstream 카테고리가 원인인지 키워드 분류.
    Strict 매칭 (word boundary / 한글 경계) 으로 false positive 차단."""
    if not verdict_text:
        return set()
    hit: set[str] = set()
    for cat, kws in _UPSTREAM_KEYWORDS.items():
        for kw in kws:
            if _kw_match(verdict_text, kw):
                hit.add(cat)
                break
    return hit


async def _classify_upstream_categories_ai(
    verdict_text: str,
    model_adapter: Any,
    *,
    cur_phase: str | None = None,
    task_name: str | None = None,
    upstream_task_names: list[str] | None = None,
) -> set[str]:
    """키워드 매칭 0건일 때 fallback. haiku 로 카테고리 분류.

    출력 스키마:
      {"categories": ["DESIGN"], "confidence": 0.85, "reasoning": "..."}
    confidence < AI_CONFIDENCE_THRESHOLD 이면 폐기.

    노드 컨텍스트(phase/task_name/upstream_task_names) 를 함께 전달하여 정확도 ↑.
    """
    if model_adapter is None or not verdict_text:
        return set()

    ctx_lines: list[str] = []
    if cur_phase:
        ctx_lines.append(f"- 현재 phase: {cur_phase}")
    if task_name:
        ctx_lines.append(f"- 실패한 작업: {task_name}")
    if upstream_task_names:
        ctx_lines.append(
            "- 같은 engagement 의 상위 TASK 후보: "
            + ", ".join(upstream_task_names[:5])
        )
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(컨텍스트 없음)"

    prompt = (
        "당신은 QA 실패 사유를 보고 어느 상위 단계 결함이 root cause 인지 분류하는 분류기입니다.\n"
        "가능한 카테고리: DESIGN, API, DB, REQ, INFRA, MOBILE, DATA\n"
        "직접적 증거가 없으면 빈 배열 반환. 추측 금지.\n\n"
        "## 노드 컨텍스트\n"
        f"{ctx_block}\n\n"
        "## QA 실패 사유\n"
        f"{verdict_text[:1500]}\n\n"
        "## 출력 형식 (JSON 만, 다른 텍스트 금지)\n"
        '{"categories": ["DESIGN"], "confidence": 0.85, "reasoning": "한 줄 사유"}'
    )
    try:
        resp = await model_adapter.call(
            model="claude-haiku-4-5-20251001",
            system="You are a precise root-cause classifier. Output JSON only.",
            prompt=prompt,
            max_tokens=300,
        )
        content = (resp.content or "").strip()
        # JSON 추출 (```json fence 제거)
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.M).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return set()
        confidence = float(parsed.get("confidence", 0.0))
        if confidence < AI_CONFIDENCE_THRESHOLD:
            logger.info(
                "ai_classify_low_confidence conf=%.2f threshold=%.2f — discarded",
                confidence, AI_CONFIDENCE_THRESHOLD,
            )
            return set()
        cats = parsed.get("categories", [])
        if not isinstance(cats, list):
            return set()
        return {c for c in cats if isinstance(c, str) and c in _VALID_CATEGORIES}
    except Exception as exc:
        logger.debug("ai_classify_failed err=%s", exc)
        return set()


_AUDIT_TABLE_ENSURED = False


async def _ensure_audit_table(db: Any) -> None:
    """upstream_rework_audit 테이블 idempotent 생성. 첫 호출 1회만.

    Migration 039 가 적용되지 않은 DB 에서도 안전하게 작동하도록 보호망.
    프로세스 라이프사이클 동안 한 번만 ALTER 시도.
    """
    global _AUDIT_TABLE_ENSURED
    if _AUDIT_TABLE_ENSURED:
        return
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS upstream_rework_audit (
              id                    TEXT PRIMARY KEY,
              qa_node_id            TEXT NOT NULL,
              detected_categories   TEXT NOT NULL,
              invalidated_node_ids  TEXT NOT NULL,
              outcome               TEXT NOT NULL DEFAULT 'pending',
              method                TEXT NOT NULL,
              notes                 TEXT,
              created_at            TEXT NOT NULL,
              resolved_at           TEXT
            )"""
        )
        _AUDIT_TABLE_ENSURED = True
    except Exception as exc:
        logger.debug("audit_table_ensure_skipped err=%s", exc)


async def _record_audit(
    db: Any,
    qa_node_id: str,
    detected_categories: set[str],
    invalidated_node_ids: list[str],
    method: str,  # 'keyword' | 'ai' | 'dry-run'
) -> None:
    """upstream_rework_audit 테이블에 결과 기록.

    Migration 039 누락 보호: 첫 호출 시 테이블 idempotent CREATE.
    """
    if db is None:
        return
    await _ensure_audit_table(db)
    try:
        await db.execute(
            """INSERT INTO upstream_rework_audit
               (id, qa_node_id, detected_categories, invalidated_node_ids,
                outcome, method, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), qa_node_id,
                json.dumps(sorted(detected_categories), ensure_ascii=False),
                json.dumps(invalidated_node_ids, ensure_ascii=False),
                "pending",  # 후속 audit 도구가 success/false_positive 로 갱신
                method, _now(),
            ),
        )
    except Exception as exc:
        logger.debug("audit_record_skipped err=%s", exc)


async def trigger_upstream_rework_if_needed(
    db: Any,
    failed_qa_node_id: str,
    failed_task_node_id: str,
    qa_verdict_text: str,
    model_adapter: Any = None,
) -> int:
    """downstream QA FAIL 시 호출. 사유에 upstream 키워드가 있으면 해당 upstream
    TASK 노드(같은 engagement 의 카테고리 매칭) 를 INVALID 로 전환.

    거시 진단 흐름:
      1. 키워드 strict 매칭 (비용 0)
      2. 0건이면 AI fallback (haiku, confidence threshold 적용) — model_adapter 필요
      3. 모드 가드: dry-run 이면 audit 만 기록, INVALID 안 함

    Returns: 영향받은 upstream 노드 수 (dry-run 모드에선 0).
    """
    if db is None or not qa_verdict_text:
        return 0

    # 킬 스위치 — 운영 즉시 차단
    if _is_killed():
        logger.info("upstream_rework_killed env=V10_UPSTREAM_REWORK_KILL — skip")
        return 0

    mode = _rework_mode()

    # ── 1단계: 키워드 매칭 (strict) ──
    categories = _classify_upstream_categories(qa_verdict_text)
    classify_method = "keyword"

    # ── 2단계: AI fallback (full 모드 + model_adapter 있을 때만) ──
    if not categories and mode == "full" and model_adapter is not None:
        # 노드 컨텍스트 수집 (정확도 향상)
        cur_phase = None
        task_name = None
        upstream_names: list[str] = []
        try:
            ctx_row = await db.fetchone(
                "SELECT n.phase, n.engagement_id, t.name AS task_name "
                "FROM nodes n LEFT JOIN nodes t ON t.id=? "
                "WHERE n.id=?",
                (failed_task_node_id, failed_qa_node_id),
            )
            if ctx_row:
                cur_phase = ctx_row.get("phase")
                task_name = ctx_row.get("task_name")
                eng_id = ctx_row.get("engagement_id")
                if eng_id:
                    up_rows = await db.fetchall(
                        "SELECT name FROM nodes WHERE engagement_id=? "
                        "AND state='COMPLETED' AND node_type='TASK' LIMIT 5",
                        (eng_id,),
                    )
                    upstream_names = [r["name"] for r in up_rows if r.get("name")]
        except Exception:
            pass

        categories = await _classify_upstream_categories_ai(
            qa_verdict_text, model_adapter,
            cur_phase=cur_phase, task_name=task_name,
            upstream_task_names=upstream_names,
        )
        classify_method = "ai" if categories else "ai_empty"

    if not categories:
        return 0

    await _ensure_rework_count_column(db)

    # downstream task 가 속한 engagement·phase 조회
    row = await db.fetchone(
        "SELECT engagement_id, phase, dag_id FROM nodes WHERE id=?",
        (failed_task_node_id,),
    )
    if not row:
        return 0
    engagement_id = row["engagement_id"]
    cur_phase = row["phase"]

    # 같은 engagement 의 COMPLETED upstream TASK 중 카테고리 매칭
    # 카테고리 → task_name 키워드 매핑 (7개 카테고리 모두 커버)
    cat_to_namekw = {
        "DESIGN": ["디자인", "design"],
        "API": ["api"],
        "DB": ["db", "데이터베이스", "스키마"],
        "REQ": ["요구사항", "백로그", "유스케이스"],
        "INFRA": ["배포", "deployment", "infra", "ci/cd"],
        "MOBILE": ["네이티브", "ios", "android", "mobile"],
        "DATA": ["피처", "모델", "파이프라인", "dataset"],
    }
    name_clauses: list[str] = []
    params: list = [engagement_id]
    for cat in categories:
        for kw in cat_to_namekw.get(cat, []):
            name_clauses.append("LOWER(task_name) LIKE ?")
            params.append(f"%{kw.lower()}%")
    if not name_clauses:
        return 0

    sql = (
        "SELECT id, task_name, phase, upstream_rework_count "
        "FROM nodes "
        f"WHERE engagement_id=? AND state='COMPLETED' "
        f"AND node_type='TASK' AND ({' OR '.join(name_clauses)})"
    )
    candidates = await db.fetchall(sql, tuple(params))
    if not candidates:
        return 0

    affected = 0
    invalidated_ids: list[str] = []
    now = _now()
    for c in candidates:
        prev_count = int(c.get("upstream_rework_count") or 0)
        if prev_count >= UPSTREAM_REWORK_LIMIT_PER_PHASE:
            logger.info(
                "upstream_rework_skipped node=%s reason=limit count=%d",
                c["id"][:8], prev_count,
            )
            continue
        # 자기 자신 또는 같은 task 는 제외
        if c["id"] == failed_task_node_id:
            continue
        verdict_meta = {
            "upstream_rework": True,
            "triggered_by_qa": failed_qa_node_id,
            "triggered_by_task": failed_task_node_id,
            "categories": sorted(categories),
            "method": classify_method,
            "reason_excerpt": qa_verdict_text[:300],
        }

        if mode == "dry-run":
            # 시뮬레이션 — INVALID 안 함, 로그만
            logger.warning(
                "upstream_rework_DRYRUN node=%s name=%s cats=%s method=%s",
                c["id"][:8], c["task_name"][:30], sorted(categories), classify_method,
            )
            affected += 1
            invalidated_ids.append(c["id"])
            continue

        try:
            # Atomic 동시성 가드: WHERE 절에 한도 재확인 — race condition 시
            # 두 hook 이 동시에 increment 하더라도 최종 한도 초과 노드는 UPDATE 0 rows.
            await db.execute(
                """UPDATE nodes
                SET state='INVALID',
                    description=?,
                    upstream_rework_count = COALESCE(upstream_rework_count,0)+1,
                    updated_at=?
                WHERE id=? AND state='COMPLETED'
                  AND COALESCE(upstream_rework_count,0) < ?""",
                (json.dumps(verdict_meta, ensure_ascii=False), now, c["id"],
                 UPSTREAM_REWORK_LIMIT_PER_PHASE),
            )
            affected += 1
            invalidated_ids.append(c["id"])
            logger.info(
                "upstream_rework_invalidated node=%s name=%s cats=%s method=%s",
                c["id"][:8], c["task_name"][:30], sorted(categories), classify_method,
            )
            # Prometheus counter (per-category)
            try:
                from engine.observability.metrics import V10_UPSTREAM_REWORK_TOTAL
                for _cat in categories:
                    V10_UPSTREAM_REWORK_TOTAL.labels(category=_cat).inc()
            except Exception:
                pass
        except Exception as e:
            logger.warning(
                "upstream_rework_failed node=%s err=%s", c["id"][:8], e,
            )

    # Audit 기록 (idempotent — 테이블 없으면 스킵)
    if affected > 0 or mode == "dry-run":
        await _record_audit(
            db, failed_qa_node_id, categories, invalidated_ids,
            method=("dry-run" if mode == "dry-run" else classify_method),
        )

    if affected > 0:
        # observability 에 이벤트 기록
        try:
            from engine.observability.events import log_event
            await log_event(
                db, "upstream_rework_triggered",
                project_id=engagement_id,
                payload={"affected": affected, "categories": sorted(categories),
                         "phase": cur_phase, "method": classify_method,
                         "mode": mode},
            )
        except Exception:
            pass
        # root cause detection counter
        try:
            from engine.observability.metrics import V10_QA_ROOT_CAUSE_DETECTED
            V10_QA_ROOT_CAUSE_DETECTED.labels(
                phase=cur_phase or "unknown", method=classify_method,
            ).inc()
        except Exception:
            pass

    final_affected = 0 if mode == "dry-run" else affected

    # post-event hook — plugin 이 cascade 결과 받아 추가 처리 (wave-engine A1 등)
    try:
        from engine.core.hook_registry import call_hooks
        await call_hooks(
            "post_upstream_rework",
            db, failed_qa_node_id, failed_task_node_id,
            qa_verdict_text, final_affected,
        )
    except Exception:
        pass

    return final_affected


# ────────────────────────────────────────────────────────────────────────
# 단일 헬퍼: executor.py 의 4개 호출 지점 (QA verdict, TASK 예외, SUSPENDED,
# BLOCKED) 이 동일한 try/except + 길이 가드 + 호출 패턴을 가지므로 중복 제거.
# ────────────────────────────────────────────────────────────────────────

MIN_DIAGNOSTIC_TEXT_LEN = 30


async def macro_diagnose_safe(
    db: Any,
    qa_node_id: str,
    task_node_id: str,
    fail_text: str,
    model_adapter: Any = None,
    *,
    source: str = "unknown",
) -> int:
    """안전한 거시 진단 호출 헬퍼 — executor.py 모든 호출 지점에서 사용.

    - 길이 가드: 30자 미만이면 즉시 0 반환 (추상 메시지 자동 skip)
    - try/except 격리: hook 실패가 호출자 흐름 차단 안 함
    - source 라벨: 호출 지점 식별 (qa_verdict | task_exception | suspended | blocked_dep)

    Returns: 영향받은 상위 노드 수.
    """
    if not fail_text or len(fail_text) < MIN_DIAGNOSTIC_TEXT_LEN:
        return 0
    try:
        affected = await trigger_upstream_rework_if_needed(
            db, qa_node_id, task_node_id, fail_text, model_adapter=model_adapter,
        )
        if affected > 0:
            logger.warning(
                "macro_diagnose_hit source=%s node=%s upstream=%d",
                source, qa_node_id[:8] if qa_node_id else "n/a", affected,
            )
        return affected
    except Exception as exc:
        logger.debug(
            "macro_diagnose_failed source=%s node=%s err=%s",
            source, qa_node_id[:8] if qa_node_id else "n/a", exc,
        )
        return 0
