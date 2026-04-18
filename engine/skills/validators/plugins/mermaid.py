"""Mermaid 다이어그램 문법·참조 무결성 검증 (내장 토크나이저 기반)."""
from __future__ import annotations

import re

from engine.skills.validators.plugins.base import (
    Validator,
    PluginValidationResult,
    Fix,
    register,
)


@register("mermaid")
class MermaidValidator(Validator):
    """Mermaid 블록 (```mermaid ... ``` 또는 <pre class="mermaid">) 검증.

    - graph/flowchart/sequenceDiagram/erDiagram 타입 존재
    - 중괄호 균형
    - 엣지 (A --> B) 의 B 가 선언되지 않으면 경고
    """
    name = "mermaid"

    _MERMAID_FENCE = re.compile(r"```mermaid\s+([\s\S]*?)```", re.IGNORECASE)
    _MERMAID_TAG = re.compile(
        r'<pre[^>]*class=["\'][^"\']*mermaid[^"\']*["\'][^>]*>([\s\S]*?)</pre>',
        re.IGNORECASE,
    )
    _NODE_RE = re.compile(r"\b([A-Za-z_][\w\-]*)\s*(?:\[|\(|>|{{|\()")
    _EDGE_RE = re.compile(r"([A-Za-z_][\w\-]*)\s*[-=]+>\s*([A-Za-z_][\w\-]*)")

    def validate(self, content: str, spec: dict, context: dict | None = None) -> PluginValidationResult:
        blocks: list[str] = []
        blocks.extend(self._MERMAID_FENCE.findall(content))
        blocks.extend(self._MERMAID_TAG.findall(content))
        if not blocks:
            return PluginValidationResult(passed=True, validator=self.name)

        failures: list[str] = []
        fixes: list[Fix] = []

        for idx, block in enumerate(blocks):
            block = block.strip()
            if not block:
                continue

            # 다이어그램 타입 확인
            first_line = block.splitlines()[0].strip().lower()
            valid_types = (
                "graph", "flowchart", "sequencediagram", "classdiagram",
                "erdiagram", "statediagram", "journey", "gantt", "pie",
            )
            if not any(first_line.startswith(t) for t in valid_types):
                failures.append(
                    f"mermaid #{idx+1}: 알려진 다이어그램 타입 선언 없음 "
                    f"(첫 줄='{first_line[:40]}')",
                )

            # 중괄호/대괄호 균형
            for open_c, close_c in [("{", "}"), ("[", "]"), ("(", ")")]:
                if block.count(open_c) != block.count(close_c):
                    failures.append(
                        f"mermaid #{idx+1}: 괄호 불균형 "
                        f"{open_c}{close_c} ({block.count(open_c)}/{block.count(close_c)})",
                    )

            # 엣지 대상 노드가 선언되었는지 (graph 계열만)
            if first_line.startswith(("graph", "flowchart")):
                declared = set(self._NODE_RE.findall(block))
                edges = self._EDGE_RE.findall(block)
                for src, dst in edges:
                    if dst not in declared:
                        failures.append(
                            f"mermaid #{idx+1}: 엣지 대상 노드 '{dst}' 미선언",
                        )

        return PluginValidationResult(
            passed=not failures,
            validator=self.name,
            failures=failures[:20],
            fixable_hints=fixes,
        )
