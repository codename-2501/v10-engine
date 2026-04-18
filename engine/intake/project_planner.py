"""
engine/intake/project_planner.py
프로젝트 Plan 자동 생성 — 인테이크 데이터를 분석하여 프로젝트 실행 계획을 수립.

역할:
  1. 인테이크 전체 데이터(핵심 기능, AS-IS, 경쟁사, 레퍼런스 분석)를 종합
  2. 프로젝트 핵심 방향, 차별화 전략, 기술 판단을 수립
  3. NODE_TEMPLATES 중 불필요한 노드를 식별 → exclude_names 반환
  4. 추가 필요한 커스텀 지시를 생성 → global_context에 주입

결과물은 raw["_project_plan"]에 저장되어 global_context → 모든 하위 노드에 전파.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def generate_project_plan(
    raw: dict,
    adapter: Any,
) -> dict:
    """인테이크 데이터를 기반으로 프로젝트 Plan을 AI로 생성.

    Args:
        raw:     인테이크 raw_json dict (reference_analysis 포함 가능).
        adapter: AI 호출용 CLIProxyAdapter.

    Returns:
        Plan dict:
        {
            "summary": "프로젝트 한 줄 요약",
            "core_direction": "핵심 방향 3~5줄",
            "differentiation": ["차별화 포인트 1", ...],
            "tech_decisions": ["기술 판단 1", ...],
            "user_priorities": ["사용자 우선순위 1", ...],
            "exclude_nodes": ["불필요 노드명 1", ...],
            "add_instructions": {"노드명": "추가 지시사항"},
            "risk_flags": ["리스크 1", ...],
            "phase_focus": {"DEFINE": "...", "DESIGN": "...", ...}
        }
    """
    if not adapter:
        logger.warning("project_planner: no AI adapter — skipping plan generation")
        return {}

    # 인테이크 데이터 요약 구성
    context_parts = []

    # 기본 정보
    project_name = raw.get("project_name") or raw.get("project_title") or "프로젝트"
    context_parts.append(f"프로젝트명: {project_name}")
    context_parts.append(f"클라이언트: {raw.get('contact_company') or raw.get('company_name') or '미정'}")

    desc = raw.get("description") or raw.get("project_desc") or ""
    if desc:
        context_parts.append(f"\n## 프로젝트 설명\n{desc[:3000]}")

    features = raw.get("core_features") or ""
    if features:
        context_parts.append(f"\n## 핵심 기능 목록\n{features[:2000]}")

    process = raw.get("current_process") or ""
    if process:
        context_parts.append(f"\n## 현행 업무 프로세스 (AS-IS)\n{process[:2000]}")

    competitors = raw.get("competitors") or ""
    if isinstance(competitors, str) and competitors:
        context_parts.append(f"\n## 경쟁사/차별화\n{competitors[:1500]}")

    kpi = raw.get("expected_kpi") or ""
    if kpi:
        context_parts.append(f"\n## 기대 KPI\n{kpi[:1000]}")

    # 레퍼런스 분석 결과
    ref_analysis = raw.get("_reference_analysis") or []
    if ref_analysis:
        ref_text = "\n## 레퍼런스 분석 결과"
        for r in ref_analysis[:3]:
            ref_text += f"\n- {r.get('url', '?')}: {r.get('content_summary', '분석 없음')[:200]}"
            if r.get("weaknesses"):
                ref_text += f"\n  약점: {', '.join(r['weaknesses'][:3])}"
        context_parts.append(ref_text)

    # scope/서비스 유형
    scopes = raw.get("scope") or raw.get("service_types") or []
    if isinstance(scopes, list):
        context_parts.append(f"\n서비스 범위: {', '.join(scopes)}")
    budget = raw.get("budget") or raw.get("budget_range") or ""
    if budget:
        context_parts.append(f"예산: {budget}")
    timeline = raw.get("timeline") or ""
    if timeline:
        context_parts.append(f"기간: {timeline}")

    context_text = "\n".join(context_parts)

    # NODE_TEMPLATES 목록 (AI가 불필요 노드를 판단할 수 있도록)
    from engine.intake.processor import NODE_TEMPLATES
    all_nodes = []
    for tmpl in NODE_TEMPLATES:
        phase = tmpl["phase"]
        for node_spec in tmpl["nodes"]:
            when = node_spec.get("when", "always")
            all_nodes.append(f"  [{phase}] {node_spec['name']} (조건: {when})")
    node_list = "\n".join(all_nodes)

    prompt = f"""당신은 AI SI 프로젝트 아키텍트입니다. 아래 인테이크 데이터를 분석하여 프로젝트 실행 계획(Plan)을 수립하세요.

{context_text}

## 사용 가능한 산출물 노드 목록
{node_list}

## 지시
아래 JSON 형식으로만 응답하세요:

{{
  "summary": "이 프로젝트의 핵심을 한 문장으로",
  "core_direction": "프로젝트 핵심 방향 3~5줄. 무엇이 가장 중요하고, 어디에 집중해야 하는지.",
  "differentiation": ["경쟁사 대비 차별화 포인트 3~5개"],
  "tech_decisions": ["기술 판단 사항 3~5개. 예: React Native vs 반응형 웹, PostgreSQL 선택 이유 등"],
  "user_priorities": ["사용자 우선순위. 예: 1순위 요양보호사(현장 즉시성), 2순위 보호자(정보 접근), 3순위 어르신(극단적 단순화)"],
  "exclude_nodes": ["이 프로젝트에 불필요한 노드명 목록. 조건에 맞지 않는 것은 이미 SKIPPED되므로, 조건이 always인데 이 프로젝트에서는 의미 없는 것만 적으세요. 빈 배열도 OK."],
  "add_instructions": {{"노드명": "해당 노드에 추가로 반영할 지시사항. 프로젝트 특수 맥락."}},
  "risk_flags": ["주의해야 할 리스크 3~5개. 예: 어르신 디지털 리터러시, 공단 API 변경 가능성 등"],
  "phase_focus": {{
    "DEFINE": "이 프로젝트에서 DEFINE 단계의 핵심 포인트",
    "DESIGN": "DESIGN 단계 핵심",
    "BUILD": "BUILD 단계 핵심",
    "VERIFY": "VERIFY 단계 핵심",
    "DELIVER": "DELIVER 단계 핵심"
  }}
}}

판단 기준:
- 고객이 제공한 핵심 기능, AS-IS, 경쟁사 분석, KPI를 모두 반영
- NODE_TEMPLATES의 노드 중 이 프로젝트에 맞지 않는 것만 exclude
- add_instructions는 프로젝트 특수 맥락이 필요한 노드에만 (예: PRD에 "공단 연동 필수 반영")
- 리스크는 실질적이고 구체적인 것만 (추상적인 "일정 리스크" 금지)"""

    try:
        resp = await adapter.call(
            model="claude-sonnet-4-6",
            prompt=prompt,
            max_tokens=2500,
            temperature=0,
        )

        # JSON 파싱
        text = resp.content
        json_match = re.search(r'```json\s*\n?([\s\S]*?)\n?```', text)
        if json_match:
            raw_json = json_match.group(1).strip()
        else:
            brace_match = re.search(r'(\{[\s\S]*\})', text)
            raw_json = brace_match.group(1) if brace_match else ""

        if raw_json:
            # 후행 쉼표 제거
            cleaned = re.sub(r',\s*([}\]])', r'\1', raw_json)
            plan = json.loads(cleaned)
            logger.info("project_plan_generated project=%s exclude=%d add_instructions=%d",
                        project_name, len(plan.get("exclude_nodes", [])),
                        len(plan.get("add_instructions", {})))
            return plan

    except Exception as e:
        logger.warning("project_plan_generation_failed error=%s", str(e))

    return {}


def plan_to_markdown(plan: dict) -> str:
    """Plan dict를 읽기 편한 마크다운으로 변환 (대시보드 표시용)."""
    if not plan:
        return ""

    lines = [f"# 프로젝트 실행 계획"]
    lines.append(f"\n**{plan.get('summary', '')}**\n")

    if plan.get("core_direction"):
        lines.append(f"## 핵심 방향\n{plan['core_direction']}\n")

    if plan.get("differentiation"):
        lines.append("## 차별화 전략")
        for d in plan["differentiation"]:
            lines.append(f"- {d}")
        lines.append("")

    if plan.get("tech_decisions"):
        lines.append("## 기술 판단")
        for t in plan["tech_decisions"]:
            lines.append(f"- {t}")
        lines.append("")

    if plan.get("user_priorities"):
        lines.append("## 사용자 우선순위")
        for u in plan["user_priorities"]:
            lines.append(f"- {u}")
        lines.append("")

    if plan.get("risk_flags"):
        lines.append("## 리스크")
        for r in plan["risk_flags"]:
            lines.append(f"- {r}")
        lines.append("")

    if plan.get("phase_focus"):
        lines.append("## 단계별 핵심")
        for phase, focus in plan["phase_focus"].items():
            lines.append(f"- **{phase}**: {focus}")
        lines.append("")

    if plan.get("exclude_nodes"):
        lines.append("## 제외 노드")
        for n in plan["exclude_nodes"]:
            lines.append(f"- ~~{n}~~")
        lines.append("")

    if plan.get("add_instructions"):
        lines.append("## 노드별 추가 지시")
        for node, instr in plan["add_instructions"].items():
            lines.append(f"- **{node}**: {instr}")

    return "\n".join(lines)
