"""
engine/ai/context_assembler.py
ContextAssembler — 5-Layer 프롬프트 컨텍스트 조립.
Layer 0: 헌법(claude_init.md) — 항상 포함, 절대 트림 불가
Layer 1: 프로젝트 글로벌 컨텍스트
Layer 2: Phase 컨텍스트
Layer 3: UpstreamDelta (Delta-First — 변경분만 전달)
Layer 4: 노드 명세 — 항상 포함, 절대 트림 불가
Layer 5: 실패 이력 (retry_count > 0 시만)
Delta 우선: 전체 문서 재전송 금지. 변경된 최소 단위만 전달.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 전체 컨텍스트 상한 (문자 기준 — 토큰 추정 후 추가 트림)
MAX_CONTEXT_CHARS = 400_000   # ≈ 100K 토큰 (4자/토큰 추정)


# ---------------------------------------------------------------------------
# 데이터 타입
# ---------------------------------------------------------------------------

@dataclass
class NodeContext:
    """ContextAssembler.assemble()에 전달하는 노드 정보."""
    node_id: str
    node_type: str
    name: str
    description: str
    phase: str
    project_id: str
    engagement_id: str
    retry_count: int
    failure_reasons: list[dict]     # [{attempt, reason, at}]
    assigned_model: str
    constitution_version_id: str | None


@dataclass
class ProjectContext:
    project_id: str
    project_name: str
    client_name: str
    project_type: str
    global_context: dict
    phase: str
    requirements_content: str = ""
    engagement_id: str = ""


@dataclass
class DeltaEntry:
    """upstream 노드의 변경분."""
    artifact_id: str
    artifact_type: str
    from_version: int
    to_version: int
    diff: str           # unified diff 또는 JSON patch
    is_empty: bool


@dataclass
class AssemblyResult:
    prompt: str
    system: str         # claude_init.md 헌법 (system 파라미터)
    estimated_tokens: int
    layers_used: list[str]


# ---------------------------------------------------------------------------
# ContextAssembler
# ---------------------------------------------------------------------------

class ContextAssembler:
    """
    Layer 0: 헌법 → system 파라미터로 분리 (절대 트림 불가)
    Layer 1~5: user 프롬프트로 조립
    트림 대상: Layer 3(UpstreamDelta) → Layer 5(실패이력) → Layer 2(Phase 설명) 순서
    Layer 0, 4는 절대 트림 안 함.
    """

    # Phase별 목표 설명
    PHASE_GOALS: dict[str, str] = {
        "DEFINE": "제품 요구사항 정의, 기능 백로그, 사용자 흐름 작성",
        "DESIGN": "시스템 아키텍처, 화면 설계, API/DB 설계, 보안 설계",
        "BUILD": "프론트엔드/백엔드 구현, DB 구축, 코드 리뷰",
        "VERIFY": "단위/통합/성능/보안 테스트, UAT, 결함 수정 순환",
        "DELIVER": "배포 계획, 데이터 이행, 매뉴얼, 운영 가이드, 인수인계",
    }

    def __init__(self, constitution_dir: str | Path = "prompts") -> None:
        self._constitution_dir = Path(constitution_dir)

    def assemble(
        self,
        node: NodeContext,
        project: ProjectContext,
        deltas: list[DeltaEntry],
        constitution_content: str | None = None,
        max_chars: int = MAX_CONTEXT_CHARS,
        patch_mode: bool = False,
        affected_sections: list[str] | None = None,
        current_artifact: str | None = None,
    ) -> AssemblyResult:
        """
        5-Layer 컨텍스트 조립.
        constitution_content: None이면 파일에서 로드.
        patch_mode: True이면 섹션 패치 모드 — 현재 문서에서 지정 섹션만 수정.
        affected_sections: patch_mode=True 시 수정 대상 섹션 헤딩 목록.
        current_artifact: patch_mode=True 시 현재(수정 전) 산출물 내용.
        """
        # Layer 0: 헌법 (system 파라미터)
        system = constitution_content or self._load_constitution(
            node.constitution_version_id
        )

        # Layer 1~5: user 프롬프트 레이어 조립
        layer1 = self._project_layer(project)
        layer2 = self._phase_layer(node.phase)
        layer3 = self._upstream_delta_layer(deltas)
        layer4 = self._node_spec_layer(
            node,
            patch_mode=patch_mode,
            affected_sections=affected_sections,
            current_artifact=current_artifact,
        )
        layer5 = self._failure_history_layer(node) if node.retry_count > 0 else ""

        layers = [
            ("L4_node_spec", layer4),    # 절대 트림 불가
            ("L1_project",   layer1),
            ("L2_phase",     layer2),
            ("L3_delta",     layer3),
            ("L5_failures",  layer5),
        ]

        prompt, used = self._trim_to_budget(layers, max_chars)

        estimated = self._estimate_tokens(system + prompt)

        logger.debug(
            "context_assembled",
            node_id=node.node_id,
            chars=len(prompt),
            estimated_tokens=estimated,
            layers=used,
        )

        return AssemblyResult(
            prompt=prompt,
            system=system,
            estimated_tokens=estimated,
            layers_used=used,
        )

    # ------------------------------------------------------------------
    # 레이어 생성
    # ------------------------------------------------------------------

    def _load_constitution(self, version_id: str | None) -> str:
        """헌법 파일 로드. 없으면 기본 헌법 반환."""
        if version_id:
            path = self._constitution_dir / f"{version_id}.md"
            if path.exists():
                return path.read_text("utf-8")
        default = self._constitution_dir / "v1" / "claude_init.md"
        if default.exists():
            return default.read_text("utf-8")
        return self._default_constitution()

    def _project_layer(self, project: ProjectContext) -> str:
        import json
        ctx = project.global_context
        parts = [
            f"## 프로젝트 정보",
            f"- 이름: {project.project_name}",
            f"- 클라이언트: {project.client_name}",
            f"- 유형: {project.project_type}",
        ]
        if project.requirements_content:
            parts.append(f"\n## 요구사항\n{project.requirements_content}")
        if ctx:
            parts.append(f"\n## 글로벌 컨텍스트\n```json\n{json.dumps(ctx, ensure_ascii=False, indent=2)}\n```")
        return "\n".join(parts)

    def _phase_layer(self, phase: str) -> str:
        goal = self.PHASE_GOALS.get(phase, phase)
        return f"## 현재 Phase: {phase}\n목표: {goal}"

    def _upstream_delta_layer(self, deltas: list[DeltaEntry]) -> str:
        """Delta-First: 변경분만 포함. 빈 delta는 제외."""
        if not deltas:
            return ""
        parts = ["## 상위 노드 변경분 (Delta-First)"]
        for d in deltas:
            if d.is_empty:
                continue
            parts.append(
                f"\n### {d.artifact_type} (v{d.from_version}→v{d.to_version})\n"
                f"```diff\n{d.diff[:5000]}\n```"  # 개별 diff 5000자 제한
            )
        return "\n".join(parts) if len(parts) > 1 else ""

    def _node_spec_layer(
        self,
        node: NodeContext,
        patch_mode: bool = False,
        affected_sections: list[str] | None = None,
        current_artifact: str | None = None,
    ) -> str:
        parts = [
            f"## 노드 명세",
            f"- ID: {node.node_id}",
            f"- 이름: {node.name}",
            f"- 유형: {node.node_type}",
            f"- Phase: {node.phase}",
        ]
        if node.description:
            parts.append(f"\n## 작업 설명\n{node.description}")

        if patch_mode and current_artifact and affected_sections:
            # 패치 모드: 현재 문서 + 수정 대상 섹션 명시
            sections_list = "\n".join(f"  - {s}" for s in affected_sections)
            parts.append(
                f"\n## 현재 문서 (패치 대상)\n"
                f"아래 문서에서 지정된 섹션만 수정하고 **전체 문서를 반환**하라.\n"
                f"지정되지 않은 섹션은 현재 내용을 그대로 유지하라.\n\n"
                f"수정 대상 섹션:\n{sections_list}\n\n"
                f"```\n{current_artifact[:30000]}\n```"
            )
            parts.append(
                "\n**[패치 모드]** 위 문서에서 지정 섹션만 업데이트하고 "
                "나머지는 원문 그대로 유지하여 완전한 문서로 반환하라. TODO 절대 금지."
            )
        elif patch_mode and current_artifact:
            # 섹션 없는 패치 모드 (폴백: 전체 문서 업데이트)
            parts.append(
                f"\n## 현재 문서\n```\n{current_artifact[:30000]}\n```"
            )
            parts.append(
                "\n상위 변경분을 반영하여 문서를 업데이트하고 완전한 문서로 반환하라. TODO 절대 금지."
            )
        else:
            parts.append("\n산출물을 완전하고 즉시 실행 가능한 형태로 생성하십시오. TODO 절대 금지.")

        return "\n".join(parts)

    def _failure_history_layer(self, node: NodeContext) -> str:
        if not node.failure_reasons:
            return ""
        parts = [f"## 실패 이력 (시도 {node.retry_count}회)"]
        for f in node.failure_reasons[-3:]:  # 최근 3회만
            parts.append(f"- 시도 {f.get('attempt', '?')}: {f.get('reason', '알 수 없음')}")
        parts.append("\n위 실패를 참고하여 동일한 오류를 반복하지 마십시오.")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 트림 + 추정
    # ------------------------------------------------------------------

    def _trim_to_budget(
        self,
        layers: list[tuple[str, str]],
        max_chars: int,
    ) -> tuple[str, list[str]]:
        """
        Layer 4(node_spec)는 무조건 포함.
        나머지는 max_chars 초과 시 역순 트림 (L3→L5→L2→L1).
        """
        # 필수 레이어 먼저
        mandatory = [(name, content) for name, content in layers if name == "L4_node_spec"]
        optional  = [(name, content) for name, content in layers if name != "L4_node_spec"]

        # 트림 우선순위 (뒤쪽부터 제거)
        trim_order = ["L5_failures", "L3_delta", "L2_phase", "L1_project"]

        chosen = list(optional)
        used_names = [name for name, _ in mandatory + chosen if _]

        def assemble_text() -> str:
            all_layers = mandatory + chosen
            return "\n\n".join(content for _, content in all_layers if content)

        while len(assemble_text()) > max_chars and trim_order:
            victim = trim_order.pop(0)
            chosen = [(n, c) for n, c in chosen if n != victim]

        return assemble_text(), [n for n, c in mandatory + chosen if c]

    def _estimate_tokens(self, text: str) -> int:
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        return int(cjk * 1.7 + (len(text) - cjk) * 0.25)

    # ------------------------------------------------------------------
    # 기본 헌법 (prompts/v1/claude_init.md 없을 때 폴백)
    # ------------------------------------------------------------------

    def _default_constitution(self) -> str:
        return """# AI 에이전트 헌법 v1.0

## 핵심 정체성
당신은 규율 집행자입니다. 규칙을 준수하고 산출물을 생성합니다.
창의적 제안, 대안 제시, 스코프 확장 — 모두 금지입니다.

## 9대 MANDATORY Rules
Rule 1: DAG 구조 강제 — 순환 참조 절대 불가
Rule 2: 위상 정렬 실행 순서만 허용
Rule 3: 모든 의존성이 COMPLETED일 때만 노드 실행 가능
Rule 4: COMPLETED 노드도 상위 변경 시 INVALID 가능
Rule 5: 영향받지 않은 노드는 절대 중단하지 않음
Rule 6: 동시 변경 시 최신 스냅샷 기준으로 충돌 해결
Rule 7: 신규 노드 추가 시 자동으로 의존성 등록 + 상태 부여
Rule 8: 모든 규칙 MANDATORY, AI 임의 해석 및 예외 금지
Rule 9: QA-Pairing 필수 — Task 노드 생성 시 QA 노드 자동 쌍 생성

## 산출물 생성 규칙
1. 지정된 파일 형식만 출력
2. 코드: 완전하고 즉시 실행 가능한 형태
3. 마크다운: 구조화된 문서 형식
4. 미완성 TODO 절대 금지

## 실패 처리
- 불가능한 작업 → BLOCKED 신호 명시
- 정보 부족 → 필요한 정보 목록 명시
- 규칙 위반 요청 → 거부 후 거부 사유 명시
"""
