from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progressive Disclosure + Constitutional AI — 2단계 생성
# ---------------------------------------------------------------------------

async def _two_phase_generate(
    model_adapter,
    model: str,
    assembly,
    max_tokens: int,
    spec: dict,
    art_type: str,
    node=None,
):
    """TASK 노드 전용 2단계 생성.

    Phase 1: 개요(outline) 생성 → 구조 검증 (0토큰)
      - 경량화: assembly.prompt 전체(60KB) 대신 핵심 스펙 + 개요 지시만 전송
      - 프론트엔드 노드는 더욱 경량화된 Phase 1 적용
    Phase 2: 본문 생성 + 자기비판 (Constitutional AI)

    실패 시 단일 호출로 폴백 (기존 방식 보장).

    Args:
        model_adapter: ModelAdapter 또는 CLIProxyAdapter
        model:         모델 ID
        assembly:      AssemblyResult (system + prompt)
        max_tokens:    최대 출력 토큰
        spec:          스킬 스펙 YAML dict
        art_type:      'document' | 'html' | 'code'
        node:          NodeSnapshot (선택적 — Phase 1 경량화에 사용)

    Returns:
        APIResponse
    """
    structural = spec.get("validation", {}).get("structural", {})
    required_headings = structural.get("required_headings", [])
    semantic_rules = spec.get("semantic", [])

    # ── Phase 1: 개요 생성 (토큰 절약 — max_tokens의 25%) ──
    # 변경 전: assembly.prompt 전체(~60KB)를 Phase 1에 그대로 전송
    # 변경 후: Phase 1은 개요 지시만 필요하므로 경량 프롬프트 사용
    #   - 프론트엔드 노드(공통 인프라/컴포넌트 구현): 노드명 + 개요 지시만 (최소화)
    #   - 기타 노드: 전체 프롬프트 앞부분(8000자) + 개요 지시 (절반 절약)

    node_name = node.name if node is not None else ""
    is_frontend_node = (
        "프론트엔드 공통 인프라" in node_name
        or "프론트엔드 컴포넌트 구현" in node_name
    )

    # 개요 지시 블록 (공통)
    outline_instruction_lines = [
        "## 지시: 개요만 먼저 작성하세요 (본문 아직 작성 금지)\n",
        "아래 형식으로 산출물의 **목차와 각 섹션별 핵심 포인트 3줄 이내**만 작성하세요:\n",
    ]
    for i, h in enumerate(required_headings, 1):
        outline_instruction_lines.append(f"{i}. **{h}**\n   - (이 섹션에서 다룰 핵심 내용 1줄)")
    outline_instruction_lines.append("\n목차만 출력하세요. 본문은 다음 단계에서 작성합니다.")
    outline_instruction = "\n".join(outline_instruction_lines)

    if is_frontend_node:
        # 프론트엔드 노드: Phase 1은 노드명 + 개요 지시만 (전체 프롬프트 전송 안 함)
        # 이유: assembly.prompt가 60KB(페이지 조립 HTML 포함)이므로 Phase 1에는 불필요
        outline_prompt = (
            f"## 산출물 개요 작성\n\n"
            f"**산출물명**: {node_name}\n\n"
            f"{outline_instruction}\n\n"
            f"위 지시에 따라 산출물의 개요(목차+구조)만 먼저 작성하세요. "
            f"실제 코드는 다음 단계에서 작성합니다."
        )
    else:
        # 기타 노드: 전체 프롬프트 앞부분(8000자)만 사용 (기존: 전체 프롬프트)
        prompt_preview = assembly.prompt[:8000]
        if len(assembly.prompt) > 8000:
            prompt_preview += "\n\n... (전체 컨텍스트는 본문 작성 단계에서 제공됩니다)"
        outline_prompt = (
            f"{prompt_preview}\n\n"
            "---\n"
            f"{outline_instruction}"
        )

    try:
        outline_resp = await model_adapter.call(
            model=model,
            system=assembly.system,
            prompt=outline_prompt,
            max_tokens=max(max_tokens // 4, 1000),
        )

        # 개요 구조 검증 (0토큰 — 프로그래밍 검증)
        outline_valid = True
        if required_headings:
            import re
            outline_text = outline_resp.content
            for heading in required_headings:
                core = re.sub(r"\s*\(.*?\)\s*", "", heading).strip()
                if not re.search(re.escape(core), outline_text, re.IGNORECASE):
                    outline_valid = False
                    break

        if not outline_valid:
            # 개요 검증 실패 → 단일 호출 폴백 (기존 방식)
            logger.info("outline_validation_failed — falling back to single-pass")
            return await model_adapter.call(
                model=model,
                system=assembly.system,
                prompt=assembly.prompt,
                max_tokens=max_tokens,
            )

    except Exception:
        # Phase 1 실패 → 단일 호출 폴백
        logger.info("outline_phase_error — falling back to single-pass")
        return await model_adapter.call(
            model=model,
            system=assembly.system,
            prompt=assembly.prompt,
            max_tokens=max_tokens,
        )

    # ── Phase 2: 본문 생성 + Constitutional AI (자기비판) ──
    constitutional_block = ""
    if semantic_rules:
        rules_text = "\n".join(f"  - {r}" for r in semantic_rules[:5])
        constitutional_block = (
            "\n\n---\n"
            "## 자기비판 (Constitutional AI — 반드시 수행)\n"
            "본문 작성 완료 후, 아래 기준으로 자가 검토하세요:\n"
            f"{rules_text}\n\n"
            "발견된 문제가 있으면 **즉시 수정**한 최종본을 출력하세요.\n"
            "문제가 없으면 그대로 출력하세요.\n"
            "자기비판 과정은 출력에 포함하지 마세요 — 최종 산출물만 출력합니다."
        )

    full_prompt = (
        f"{assembly.prompt}\n\n"
        "---\n"
        f"## 참고: 사전 승인된 개요\n"
        f"```\n{outline_resp.content}\n```\n\n"
        "위 개요 구조를 **정확히 따라** 전체 산출물을 작성하세요.\n"
        "개요의 섹션 순서와 구성을 변경하지 마세요."
        f"{constitutional_block}"
    )

    response = await model_adapter.call(
        model=model,
        system=assembly.system,
        prompt=full_prompt,
        max_tokens=max_tokens,
    )

    return response
