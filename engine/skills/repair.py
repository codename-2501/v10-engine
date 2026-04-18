from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


def _build_repair_context(content: str, issues: list, spec: dict) -> tuple[str, bool]:
    """보정 요청에 사용할 산출물 컨텍스트를 최적화하여 반환.

    이슈 유형에 따라 두 가지 전략 중 하나를 선택:

    전략 A — 섹션 타게팅 (토큰 절약):
      적용 조건: 모든 이슈가 "필수 섹션 누락"인 경우.
      방법: PASS 섹션은 헤딩 + 첫 200자 요약, FAIL(누락) 섹션은 명시적 추가 지시.
      효과: 전체 15K 대신 ~5K 전송.

    전략 B — 전체 전송 (안전 폴백):
      적용 조건: 분량 미달/초과, 테이블/섹션 부족, 금지 패턴, 코드 블록 등
               문서 전체가 필요한 이슈.
      방법: content[:15000] 그대로.

    Returns:
        (repair_context, used_section_targeting)
    """
    import re as _re

    # ── 전략 B 강제 조건 ──────────────────────────────────────────────────
    # 아래 키워드가 포함된 이슈는 섹션 타게팅 불가 (문서 전체 필요)
    FULL_CONTENT_TRIGGERS = (
        "최소 분량 미달",   # AI가 전체를 보고 내용 확장
        "최대 분량 초과",   # AI가 전체를 보고 내용 축소
        "테이블 부족",      # 테이블은 문서 전체에 분산
        "섹션 부족",        # 섹션 개수는 문서 전체 기준
        "필수 코드 블록",   # 코드 블록 위치 불특정
        "금지 패턴",        # 금지 키워드는 어디든 있을 수 있음
    )
    if any(
        trigger in iss
        for iss in issues
        for trigger in FULL_CONTENT_TRIGGERS
    ):
        return content[:15000], False

    # ── 누락 섹션명 추출 ──────────────────────────────────────────────────
    missing_headings: list[str] = []
    for iss in issues:
        m = _re.match(r"필수 섹션 누락: '(.+)'", iss)
        if m:
            missing_headings.append(m.group(1))

    if not missing_headings:
        # 알 수 없는 이슈 유형 → 안전하게 전체 전송
        return content[:15000], False

    # ── 헤딩 기준으로 섹션 분리 ───────────────────────────────────────────
    heading_re = _re.compile(r'^(#{1,3})\s+(.+)$', _re.MULTILINE)
    splits = list(heading_re.finditer(content))

    if not splits:
        # 헤딩 없는 문서 → 전체 전송 (섹션 타게팅 불가)
        return content[:15000], False

    # 프리앰블 (첫 헤딩 이전 텍스트)
    preamble = content[:splits[0].start()].strip()

    # 섹션 목록: {heading, level, body}
    sections = []
    for i, hit in enumerate(splits):
        body_start = hit.end()
        body_end = splits[i + 1].start() if i + 1 < len(splits) else len(content)
        sections.append({
            "heading": hit.group(2).strip(),
            "level":   len(hit.group(1)),
            "body":    content[body_start:body_end].strip(),
        })

    # ── 섹션별 PASS/FAIL 분류 ─────────────────────────────────────────────
    # missing_headings의 각 항목이 실제 content의 어느 섹션에 해당하는지 확인.
    # StructureValidator와 동일한 fuzzy 매칭 로직 사용.
    def _is_missing(heading_text: str) -> bool:
        for mh in missing_headings:
            core = _re.sub(r"\s*\(.*?\)\s*", "", mh).strip()
            if core and core in heading_text:
                return True
        return False

    parts: list[str] = []

    # 프리앰블: 최대 300자 (문서 맥락 유지)
    if preamble:
        parts.append(preamble[:300])

    for sec in sections:
        hashes = "#" * sec["level"]
        heading_text = sec["heading"]
        body = sec["body"]

        if _is_missing(heading_text):
            # 이 섹션이 FAIL 판정을 받았다는 것은 실제로는 content에 존재하지만
            # validator 매칭에 실패했거나, 내용이 비어 있는 경우.
            # → 섹션 본문 최대 1500자 전체 제공 (AI가 내용 보완)
            if body:
                parts.append(f"{hashes} {heading_text}\n{body[:1500]}")
            else:
                parts.append(f"{hashes} {heading_text}\n⚠️ 이 섹션이 비어 있음 — 아래 지시에 따라 내용을 추가하세요.")
        else:
            # PASS 섹션: 헤딩 + 첫 200자 요약 (문서 구조 파악용)
            summary = body[:200].replace("\n", " ").strip()
            ellipsis = "…" if len(body) > 200 else ""
            parts.append(
                f"{hashes} {heading_text}\n"
                f"✅ PASS — 변경 금지. ({len(body)}자) {summary}{ellipsis}"
            )

    # 명시적 누락 섹션 가이드 (누락 섹션이 content에 아예 없는 경우)
    #   → sections 루프에서 걸리지 않으므로 별도 안내
    existing_headings_text = " ".join(s["heading"] for s in sections)
    truly_absent = []
    for mh in missing_headings:
        core = _re.sub(r"\s*\(.*?\)\s*", "", mh).strip()
        if core and core not in existing_headings_text:
            truly_absent.append(mh)

    if truly_absent:
        parts.append("\n## ⚠️ 완전히 누락된 섹션 — 반드시 신규 추가")
        for mh in truly_absent:
            parts.append(f"- **{mh}**: 이 섹션 자체가 없음. 올바른 헤딩으로 추가 필요.")

    result = "\n\n".join(parts)

    # 결과가 너무 짧으면 파싱 실패로 간주 → 전체 폴백
    if len(result) < 300:
        return content[:15000], False

    return result[:8000], True


async def _auto_repair_artifact(
    model_adapter, model: str, content: str,
    issues: list, spec: dict, max_tokens: int,
    node: "NodeSnapshot",
) -> Optional[str]:
    """검증 실패한 산출물을 AI에게 보정 요청.

    Args:
        content:    원본 산출물
        issues:     검증 실패 사유 목록 (validators.py 생성 형식)
        spec:       스킬 스펙 (validation 규칙 포함)
        max_tokens: 최대 출력 토큰

    Returns:
        보정된 산출물 문자열, 또는 보정 불가 시 None.
    """
    issues_text = "\n".join(f"- {iss}" for iss in issues)

    # 분량 부족이면 max_tokens 상향
    repair_max = max_tokens
    for iss in issues:
        if "최소 분량 미달" in iss:
            repair_max = min(max_tokens * 2, 32000)
            break

    # 이슈 유형에 따라 최적 컨텍스트 선택
    # - 섹션 누락만: 타게팅 (~5K) / 기타: 전체 (~15K)
    repair_context, used_targeting = _build_repair_context(content, issues, spec)

    if used_targeting:
        context_label = "현재 산출물 (PASS 섹션 요약 / FAIL 섹션 집중 표시)"
        extra_instruction = (
            "- ✅ PASS로 표시된 섹션은 동일한 내용으로 유지하세요 (임의 변경 금지).\n"
            "- ⚠️ 표시된 섹션을 보완/추가하세요.\n"
        )
    else:
        context_label = "현재 산출물"
        extra_instruction = ""

    prompt = f"""아래 산출물이 품질 검증에 실패했습니다. 실패 사유를 보고 산출물을 보완하세요.

## 검증 실패 사유
{issues_text}

## {context_label}
{repair_context}

## 지시
- 실패 사유에 해당하는 부분만 보완하세요.
{extra_instruction}- 나머지 내용은 그대로 유지하세요.
- 산출물 전체를 다시 출력하세요 (보완된 부분만이 아니라 전체).
- TODO, TBD, 미정 단어 사용 금지.
- 원본 형식(마크다운, JSON 등)을 유지하세요."""

    try:
        resp = await model_adapter.call(
            model=model,
            system="산출물 품질 보정 전문가입니다. 검증 실패 사유를 정확히 해결하세요.",
            prompt=prompt,
            max_tokens=repair_max,
        )
        repaired = resp.content
        if len(repaired) > len(content) * 0.5:  # 너무 짧으면 보정 실패로 판단
            logger.info(
                "auto_repair_attempted node_id=%s original=%d repaired=%d targeting=%s",
                node.id, len(content), len(repaired), used_targeting,
            )
            return repaired
    except Exception as e:
        logger.warning("auto_repair_failed node_id=%s error=%s", node.id, str(e))

    return None


async def _defect_cascade(db, node, test_content: str, model_adapter) -> None:
    """VERIFY 산출물에서 결함을 분석하고, 해당 BUILD 노드를 INVALID 처리.

    동작:
      1. AI에게 테스트 결과에서 결함 목록 추출 요청 (haiku — 저렴)
      2. 결함이 "수정 필요" 수준이면 관련 상위 노드를 INVALID 처리
      3. DAG 재큐 → 상위 노드 재실행 → 수정된 산출물로 재테스트

    코어 불변: DAGAdvancer/StateMachine/cascade.py 미변경.
    """
    import shutil
    from engine.ai.model_adapter import CLIProxyAdapter

    cli = shutil.which("claude")
    if not cli:
        return

    # 상위 노드 목록 (결함 대상 특정용)
    upstream_nodes = await db.fetchall(
        """SELECT n.id, n.name, n.phase FROM nodes n
           JOIN dags d ON n.dag_id = d.id
           WHERE d.project_id = ? AND n.phase IN ('BUILD', 'DESIGN')
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'""",
        (node.project_id,),
    )
    node_list_str = "\n".join(f"  - [{n['phase']}] {n['name']}" for n in upstream_nodes)

    # 테스트 결과에서 결함 분석 (haiku — 빠르고 저렴)
    try:
        analyzer = CLIProxyAdapter(cli)
        judge_prompt = f"""아래 테스트 결과 산출물에서 결함을 분석하세요.

{test_content[:3000]}

상위 산출물 목록 (결함 원인 특정용):
{node_list_str}

아래 JSON 형식으로만 응답하세요:
{{"has_critical_defects": true 또는 false, "defect_count": 숫자, "defect_summary": "요약", "affected_nodes": ["결함 원인인 산출물 이름만"], "requires_fix": true 또는 false}}

판단 기준:
- 테스트 결과가 "통과/합격" 위주이면: requires_fix=false, affected_nodes=[]
- 결함이 있지만 경미하면: requires_fix=false
- 심각한 결함이면: requires_fix=true, affected_nodes에 결함 원인 산출물 이름만 포함
- affected_nodes는 위 목록에 있는 이름만 사용 (없는 이름 금지)
- 테스트 문서 자체의 문제(형식 불완전 등)는 결함이 아님 — requires_fix=false"""

        resp = await analyzer.call(
            model="claude-haiku-4-5-20251001",
            prompt=judge_prompt, max_tokens=200, temperature=0,
        )

        import re, json as _json
        json_match = re.search(r'\{[^}]+\}', resp.content)
        if not json_match:
            return

        analysis = _json.loads(json_match.group())

        defect_count = analysis.get("defect_count", 0)
        if not analysis.get("requires_fix", False) or defect_count == 0:
            logger.info("defect_cascade_not_needed node_id=%s defects=%d",
                        node.id, defect_count)
            return

        # 결함 있음 → 해당 VERIFY 노드도 재실행 대상 (수정 후 재검증)
        logger.info("defect_cascade_triggered node_id=%s defects=%d",
                    node.id, defect_count)

        affected_names = analysis.get("affected_nodes", [])
        if not affected_names:
            return

        # 결함 원인 노드만 INVALID 처리 (이름 매칭)
        now = _now()
        affected_phase = "BUILD"  # 기본값 — 대부분 BUILD 단계 산출물이 결함 원인
        all_upstream = await db.fetchall(
            """SELECT n.id, n.name, n.phase, n.failure_reasons FROM nodes n
               JOIN dags d ON n.dag_id = d.id
               WHERE d.project_id = ? AND n.node_type = 'TASK'
                 AND n.state = 'COMPLETED'""",
            (node.project_id,),
        )
        # 이름 매칭 (정확 매칭 우선, 부분 매칭 폴백)
        targets = []
        for n in all_upstream:
            matched = False
            for aname in affected_names:
                if n["name"] == aname:  # 정확 매칭
                    matched = True
                    break
                if len(aname) >= 4 and aname in n["name"]:  # 부분 매칭 (4자 이상만)
                    matched = True
                    break
            if not matched:
                continue
            # 순환 방지: 이 노드가 테스트 결함으로 이미 2회 이상 INVALID 됐으면 건너뜀
            import json as _j2
            try:
                fr = _j2.loads(n["failure_reasons"] or "[]")
            except (ValueError, TypeError):
                fr = []
            cascade_count = sum(1 for f in fr if "테스트 결함" in f.get("reason", ""))
            if cascade_count >= 2:
                logger.warning(
                    "defect_cascade_limit node=%s cascades=%d — skipping (max 2)",
                    n["name"], cascade_count,
                )
                continue
            targets.append(n)

        invalidated = 0
        for t in targets:
            await db.execute(
                """UPDATE nodes SET state='INVALID',
                   failure_reasons=json_insert(COALESCE(failure_reasons,'[]'), '$[#]',
                     json_object('attempt', 0, 'reason', ?, 'at', ?)),
                   updated_at=?, version=version+1
                   WHERE id=?""",
                (f"테스트 결함 발견: {analysis.get('defect_summary', '')}", now, now, t["id"]),
            )
            invalidated += 1

        if invalidated > 0:
            # affected_phase 결정 (INVALID된 노드 중 가장 많은 phase)
            phase_counts: dict[str, int] = {}
            for t in targets:
                p = t.get("phase", "BUILD")
                phase_counts[p] = phase_counts.get(p, 0) + 1
            affected_phase = max(phase_counts, key=phase_counts.get) if phase_counts else "BUILD"

            logger.info(
                "defect_cascade_triggered node_id=%s phase=%s invalidated=%d summary=%s",
                node.id, affected_phase, invalidated,
                analysis.get("defect_summary", "")[:100],
            )

            # QA 쌍 노드도 INVALID 처리 (수정 후 재검증 필요)
            for t in targets:
                qa_row = await db.fetchone(
                    "SELECT qa_pair_node_id FROM nodes WHERE id=?", (t["id"],)
                )
                if qa_row and qa_row["qa_pair_node_id"]:
                    await db.execute(
                        "UPDATE nodes SET state='INVALID', updated_at=?, version=version+1 WHERE id=? AND state='COMPLETED'",
                        (now, qa_row["qa_pair_node_id"]),
                    )

            # VERIFY→BUILD 게이트도 INVALID (재통과 필요)
            await db.execute(
                """UPDATE nodes SET state='INVALID', updated_at=?, version=version+1
                   WHERE project_id=? AND node_type='GATE'
                     AND name LIKE '%BUILD%VERIFY%' AND state='COMPLETED'""",
                (now, node.project_id),
            )

            # 이 VERIFY 노드를 실패 처리 → DAGAdvancer가 retry하면서 DAG 재큐
            raise ValueError(
                f"결함 {analysis.get('defect_count', 0)}건 발견 → {affected_phase} 단계로 반려: "
                f"{analysis.get('defect_summary', '')}"
            )

    except ValueError:
        # ValueError는 의도적 cascade → 상위로 전파 (DAGAdvancer가 retry 처리)
        raise
    except Exception as exc:
        # 결함 분석 자체 실패는 치명적이지 않음 — 로그만 남김
        logger.warning("defect_cascade_error node_id=%s error=%s", node.id, str(exc))
