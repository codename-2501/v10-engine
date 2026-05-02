from __future__ import annotations

import json
import logging
from typing import Any, Optional

from engine.skills.utils import _now

logger = logging.getLogger(__name__)


_QA_OUTPUT_SCHEMA = """\
반드시 아래 JSON 형식으로만 응답하세요. JSON 외 텍스트 금지.
```json
{
  "summary": "PASS" 또는 "FAIL",
  "score": 0~100 정수,
  "categories": [
    {
      "name": "카테고리명",
      "result": "PASS" 또는 "FAIL",
      "issues": [
        {
          "title": "이슈 제목",
          "severity": "CRITICAL" 또는 "HIGH" 또는 "MEDIUM" 또는 "LOW",
          "description": "상세 설명",
          "expected": "기대 결과",
          "actual": "실제 결과",
          "suggested_fix": "수정 제안"
        }
      ]
    }
  ]
}
```

채점 기준 (가중합 100점):
- 기능 완성도 (30%): 필수 섹션/기능 누락, 요구사항 충족 여부
- 데이터 정합성 (20%): 수치/테이블/참조의 일관성, 교차 검증
- 상태 일관성 (15%): 용어/표기 통일, 버전/날짜 정합, 문서 간 충돌 없음
- 보안/규정 (15%): 민감정보 노출, 규정 준수, 접근 제어 명시
- 품질/완성도 (10%): 분량 적정성, 깊이, 실행 가능성
- 기타 (10%): 가독성, 포맷 준수, 금지 키워드 부재

판정 규칙:
- score < 85 → "FAIL"
- CRITICAL 이슈 1건 이상 → 즉시 "FAIL"
- 그 외 → "PASS"
"""


async def _build_qa_ai_prompt(
    db: Any,
    node: "NodeSnapshot",
    spec: dict,
    project: "ProjectContext",
    task_content_cache: Optional[str] = None,
) -> str:
    """QA 노드 전용 AI 프롬프트 빌드.

    1. 쌍(pair) TASK 산출물을 로드하여 검토 대상으로 주입
    2. qa_prompt가 있으면 사용, 없으면 기본 QA 프롬프트 생성
    3. semantic criteria를 {{semantic_criteria}} 자리에 주입
    4. 구조화된 JSON 출력 스키마를 강제
    """
    from engine.skills.executor import _load_task_artifact, _sample_code_files
    from engine.skills.template import render

    # 검토 대상 산출물 로드 (캐시가 있으면 재사용 — 항상 qa_mode 예산)
    task_content = task_content_cache if task_content_cache is not None else await _load_task_artifact(db, node.task_pair_node_id, qa_mode=True)
    task_name = node.name.replace("[QA] ", "")

    # semantic criteria 구성
    semantic_rules = spec.get("validation", {}).get("semantic", [])
    semantic_text = ""
    if semantic_rules:
        semantic_text = "\n".join(f"  - {r}" for r in semantic_rules)

    # qa_prompt 사용 (있으면), 없으면 기본 생성
    qa_template = spec.get("qa_prompt", "")
    if qa_template:
        variables = {
            "name": node.name,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "phase": node.phase,
            "semantic_criteria": semantic_text or "(의미 검증 기준 없음)",
        }
        qa_body = render(qa_template, variables)
    else:
        qa_body = f"아래 산출물 '{task_name}'을(를) 검토하세요.\n"
        if semantic_text:
            qa_body += f"\n검증 기준:\n{semantic_text}\n"

    # structural 검증 기준도 명시
    structural = spec.get("validation", {}).get("structural", {})
    struct_checks = []
    if structural.get("required_headings"):
        struct_checks.append(
            "필수 섹션: " + ", ".join(structural["required_headings"])
        )
    if structural.get("required_tables"):
        struct_checks.append(f"최소 테이블: {structural['required_tables']}개")
    if structural.get("min_chars"):
        struct_checks.append(f"최소 분량: {structural['min_chars']}자")
    if structural.get("forbidden"):
        struct_checks.append(
            "금지 키워드: " + ", ".join(structural["forbidden"])
        )

    # 최종 프롬프트 조립
    prompt = f"# QA 검증: {task_name}\n"
    prompt += f"프로젝트: {project.project_name} ({project.client_name})\n"
    prompt += f"단계: {node.phase}\n\n"

    if task_content:
        # 대형 코드 산출물: 헤더(라우트+파일목록) + 파일별 샘플링으로 전체 구조 파악
        if len(task_content) > 30000 and spec.get("type") == "code":
            header = task_content[:5000]
            file_samples = _sample_code_files(task_content, max_total=10000)
            if file_samples:
                task_content = (
                    header
                    + f"\n\n## 코드 파일별 구조 샘플 (전체 {len(task_content)}자 중 주요 부분)\n"
                    + file_samples
                )
                logger.info(
                    "qa_code_sampling original=%d sampled=%d",
                    len(task_content), len(header) + len(file_samples),
                )
            else:
                task_content = task_content[:16000] + f"\n\n... (전체 {len(task_content)}자 중 16,000자만 표시)"
        else:
            # 기존 로직: 문서형/소형 코드
            safety_cap = 16000 if len(task_content) > 6000 else 6000
            if len(task_content) > safety_cap:
                task_content = task_content[:safety_cap] + f"\n\n... (전체 {len(task_content)}자 중 {safety_cap:,}자만 표시)"
        prompt += f"## 검토 대상 산출물\n```\n{task_content}\n```\n\n"
    else:
        prompt += "## 검토 대상 산출물\n(산출물 없음 — TASK가 아직 미완료)\n\n"

    prompt += f"## 검증 지시\n{qa_body}\n\n"

    if struct_checks:
        prompt += "## 프로그래매틱 검증 기준 (자동 검증 실패 항목 포함)\n"
        for sc in struct_checks:
            prompt += f"- {sc}\n"
        prompt += "\n"

    # S4-2 Layer C: 자기확인 절차 — hallucination("섹션 누락" 오판) 감소용 CoT.
    # AI 가 긴 문서를 읽고도 섹션을 못 찾는 거짓 FAIL 이 실전에서 관찰됨.
    # 섹션 나열을 먼저 시키면 본문 읽기가 선행되어 오판 확률 감소.
    prompt += (
        "\n## 판정 절차 (엄수)\n"
        "1. 산출물의 모든 `##` 시작 헤더를 번호 매겨 빠짐없이 나열하세요.\n"
        "2. 각 헤더 섹션의 대략적 본문 길이(문단·표 수)를 확인하세요.\n"
        "3. 그 후에 섹션 누락·미완성 여부를 판단하세요.\n"
        "4. \"X 섹션 누락\"이라 주장하려면 X 가 1번 목록에 **없음**을 먼저 확인하세요.\n\n"
    )

    prompt += f"## 출력 형식 (엄격 준수)\n{_QA_OUTPUT_SCHEMA}\n"

    # post-event hook — plugin 이 prompt 강화 가능 (wave-engine A4 등)
    try:
        from engine.core.hook_registry import call_hooks
        results = await call_hooks(
            "post_qa_prompt", db, node, spec, project, prompt,
        )
        # hook 가 string 반환 시 마지막 non-None 결과로 교체
        for r in results:
            if isinstance(r, str) and r:
                prompt = r
    except Exception:
        pass

    return prompt


def _parse_qa_verdict(content: str) -> dict:
    """AI QA 응답에서 구조화된 판정 JSON을 추출.

    JSON 파싱 실패 시 보수적으로 FAIL 판정 (비결정적 결과 방지).
    """
    import re as _re

    # JSON 블록 추출 (```json ... ``` 또는 { ... })
    json_match = _re.search(r'```json\s*\n?([\s\S]*?)\n?```', content)
    if json_match:
        raw = json_match.group(1).strip()
    else:
        # 중괄호 블록 직접 추출
        brace_match = _re.search(r'(\{[\s\S]*\})', content)
        raw = brace_match.group(1) if brace_match else ""

    if raw:
        # 1차: 직접 파싱
        # 2차: 후행 쉼표/주석 제거 후 재시도
        for attempt_raw in [raw, _re.sub(r',\s*([}\]])', r'\1', _re.sub(r'//.*$', '', raw, flags=_re.MULTILINE))]:
            try:
                verdict = json.loads(attempt_raw)
                if "summary" in verdict and "score" in verdict:
                    # CRITICAL 이슈 존재 시 강제 FAIL (Zero Tolerance)
                    for cat in verdict.get("categories", []):
                        for iss in cat.get("issues", []):
                            if iss.get("severity") == "CRITICAL":
                                verdict["summary"] = "FAIL"
                    # score < 85 → 강제 FAIL
                    if isinstance(verdict["score"], (int, float)) and verdict["score"] < 85:
                        verdict["summary"] = "FAIL"
                    return verdict
            except (json.JSONDecodeError, TypeError):
                continue

    # 폴백: 텍스트에서 PASS/FAIL + score 추출
    logger.warning("qa_verdict_parse_failed — falling back to text scan")
    # score 숫자 추출 시도
    score_match = _re.search(r'"?score"?\s*[:=]\s*(\d+)', content)
    fallback_score = int(score_match.group(1)) if score_match else None

    if "FAIL" not in content.upper() and "PASS" in content.upper():
        return {"summary": "PASS", "score": fallback_score or 85, "categories": []}
    if fallback_score is not None and fallback_score >= 85 and "FAIL" not in content.upper():
        return {"summary": "PASS", "score": fallback_score, "categories": []}

    # 기본값: 파싱 실패 = FAIL (보수적 판정)
    return {
        "summary": "FAIL",
        "score": 0,
        "categories": [{
            "name": "파싱 실패",
            "result": "FAIL",
            "issues": [{
                "title": "QA 출력 파싱 실패",
                "severity": "HIGH",
                "description": "AI QA 응답이 요구 JSON 형식에 맞지 않음",
                "expected": "구조화된 JSON 판정",
                "actual": content[:200],
                "suggested_fix": "QA 노드 재실행",
            }],
        }],
    }


def _verdict_to_markdown(raw_content: str) -> str:
    """AI QA verdict 응답을 읽을 수 있는 마크다운 리포트로 변환.

    JSON 파싱 성공 시 구조화된 마크다운, 실패 시 원본 그대로 반환.
    """
    verdict = _parse_qa_verdict(raw_content)

    lines = []
    summary = verdict.get("summary", "?")
    score = verdict.get("score", "?")
    lines.append(f"# QA 판정: {summary} (점수: {score}/100)")
    lines.append("")

    for cat in verdict.get("categories", []):
        cat_name = cat.get("name", "카테고리")
        cat_result = cat.get("result", "?")
        lines.append(f"## {cat_name} — {cat_result}")
        issues = cat.get("issues", [])
        if not issues:
            lines.append("이슈 없음.")
        for iss in issues:
            sev = iss.get("severity", "?")
            title = iss.get("title", "제목 없음")
            lines.append(f"### [{sev}] {title}")
            if iss.get("description"):
                lines.append(f"**설명:** {iss['description']}")
            if iss.get("expected"):
                lines.append(f"**기대:** {iss['expected']}")
            if iss.get("actual"):
                lines.append(f"**실제:** {iss['actual']}")
            if iss.get("suggested_fix"):
                lines.append(f"**수정 제안:** {iss['suggested_fix']}")
            lines.append("")
        lines.append("")

    # 원본 JSON도 접어서 포함
    lines.append("<details><summary>원본 JSON</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(verdict, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("</details>")

    return "\n".join(lines)


async def _save_qa_stamp(db: Any, node: "NodeSnapshot", verdict: dict) -> None:
    """QA 판정 결과를 artifact_qa_stamps 테이블에 저장."""
    import uuid as _uuid

    # 쌍 TASK의 artifact_id 조회
    art_row = await db.fetchone(
        "SELECT id FROM artifacts WHERE node_id=?",
        (node.task_pair_node_id,),
    )
    if not art_row:
        logger.warning("qa_stamp_skip — no artifact for task_node_id=%s", node.task_pair_node_id)
        return

    now = _now()
    verdict_str = "PASS" if verdict["summary"] == "PASS" else "CONDITIONAL_PASS"
    # DB verdict는 PASS/CONDITIONAL_PASS만 허용 — FAIL이면 stamp을 저장하지 않음
    # (FAIL 시 ValueError로 재시도되므로 stamp은 최종 통과 시에만 기록)
    if verdict["summary"] == "FAIL":
        return

    # phase CHECK 제약 매핑 — DB에 허용된 값만 사용
    # CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY'))
    _PHASE_MAP = {
        "DEFINE": "PLANNING", "BUILD": "DEVELOPMENT", "VERIFY": "DEVELOPMENT",
        "DELIVER": "DELIVERY", "PLANNING": "PLANNING", "DESIGN": "DESIGN",
        "DEVELOPMENT": "DEVELOPMENT", "INFRASTRUCTURE": "INFRASTRUCTURE",
        "DELIVERY": "DELIVERY", "API_SERVER": "API_SERVER",
    }
    db_phase = _PHASE_MAP.get(node.phase, "DEVELOPMENT")

    stamp_id = str(_uuid.uuid4())
    try:
        await db.execute(
            """INSERT OR REPLACE INTO artifact_qa_stamps
               (id, artifact_id, phase, qa_node_id, verdict, stamped_at,
                verification_output)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (stamp_id, art_row["id"], db_phase, node.id,
             verdict_str, now, json.dumps(verdict, ensure_ascii=False)),
        )
        # artifact_versions.is_qa_approved 업데이트
        await db.execute(
            """UPDATE artifact_versions SET is_qa_approved=1
               WHERE artifact_id=? AND version_num = (
                 SELECT MAX(version_num) FROM artifact_versions WHERE artifact_id=?
               )""",
            (art_row["id"], art_row["id"]),
        )
        logger.info(
            "qa_stamp_saved artifact_id=%s verdict=%s score=%s",
            art_row["id"], verdict_str, verdict.get("score"),
        )
    except Exception as exc:
        logger.warning("qa_stamp_save_failed error=%s", str(exc))


def _build_self_check_block(spec: dict) -> str:
    """Generate a self-validation instruction block from spec rules.

    Appended to the TASK prompt so that the LLM verifies its own output
    before responding.

    Args:
        spec: The full skill spec dict (must contain ``validation.structural``).

    Returns:
        A markdown-formatted self-check instruction string.
    """
    structural = spec.get("validation", {}).get("structural", {})
    if not structural:
        return ""

    lines = [
        "\n\n---\n",
        "## 자체 검증 체크리스트",
        "작성 완료 후 아래 항목을 반드시 확인하세요:\n",
    ]

    required_headings = structural.get("required_headings", [])
    if required_headings:
        lines.append("### 필수 섹션")
        for heading in required_headings:
            lines.append(f"- [ ] **{heading}** 섹션이 포함되어 있는가?")

    required_tables = structural.get("required_tables")
    if required_tables:
        lines.append(f"\n### 필수 테이블")
        lines.append(f"- [ ] 최소 {required_tables}개의 테이블이 포함되어 있는가?")

    min_sections = structural.get("min_sections")
    if min_sections:
        lines.append(f"\n### 최소 섹션 수")
        lines.append(f"- [ ] 최소 {min_sections}개의 ## 섹션이 있는가?")

    min_chars = structural.get("min_chars")
    if min_chars:
        lines.append(f"\n### 최소 분량")
        lines.append(f"- [ ] 총 {min_chars}자 이상 작성되었는가?")

    has_code_blocks = structural.get("has_code_blocks")
    if has_code_blocks:
        lines.append("\n### 코드 블록")
        lines.append("- [ ] 코드 블록(```)이 포함되어 있는가?")

    return "\n".join(lines)
