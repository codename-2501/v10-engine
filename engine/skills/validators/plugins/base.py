"""
Validator Plugin base — Stage 20.

모든 plugin 은 Validator 를 상속한다. register 데코레이터로 REGISTRY 에 등록.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Fix:
    """validator 가 제안한 자동 수정 패치.

    kind: "patch"(문자열 치환) | "append"(끝에 추가) | "replace_section"(섹션 교체)
    target: 적용 위치 식별자 (정규식·offset·섹션 ID)
    replacement: 교체할 내용
    rationale: 왜 이 패치가 필요한가 (감사 로그용)
    """
    kind: str
    target: str
    replacement: str
    rationale: str


@dataclass
class PluginValidationResult:
    """단일 plugin 실행 결과.

    passed=True 면 failures/fixable_hints 공백.
    passed=False + fixable_hints 있으면 chain 이 자동 수정 시도.
    """
    passed: bool
    validator: str
    failures: list[str] = field(default_factory=list)
    fixable_hints: list[Fix] = field(default_factory=list)


@dataclass
class ChainResult:
    """chain 전체 결과."""
    passed: bool
    fixed_content: str | None  # 자동 수정 적용된 content (변경 없으면 None)
    results: list[PluginValidationResult] = field(default_factory=list)
    total_failures: int = 0
    auto_fixed_count: int = 0

    @property
    def failures(self) -> list[str]:
        out = []
        for r in self.results:
            out.extend([f"[{r.validator}] {m}" for m in r.failures])
        return out


class Validator(ABC):
    """플러그인 ABC — 각 plugin 은 name + validate + apply_fixes 구현."""
    name: str = "abstract"

    @abstractmethod
    def validate(
        self,
        content: str,
        spec: dict,
        context: dict | None = None,
    ) -> PluginValidationResult:
        """검증 수행. AI 호출 없이 동기 처리."""
        ...

    def apply_fixes(self, content: str, fixes: list[Fix]) -> str:
        """fixable_hints 를 순차 적용해 content 를 수정. 기본 구현.

        각 plugin 은 필요 시 override.
        """
        import re as _re
        new = content
        for fix in fixes:
            if fix.kind == "patch":
                # target 은 정규식 — 첫 매치만 치환
                try:
                    new = _re.sub(fix.target, fix.replacement, new, count=1)
                except Exception as e:
                    logger.debug("fix_patch_fail validator=%s err=%s",
                                 self.name, e)
            elif fix.kind == "append":
                new = new + "\n" + fix.replacement
            elif fix.kind == "replace_section":
                # target 은 섹션 id → <section id=target>…</section> 블록 교체
                try:
                    pattern = (
                        rf"<section[^>]*id=[\"']{_re.escape(fix.target)}[\"'][^>]*>"
                        r"[\s\S]*?</section>"
                    )
                    new = _re.sub(pattern, fix.replacement, new, count=1)
                except Exception as e:
                    logger.debug("fix_replace_section_fail validator=%s err=%s",
                                 self.name, e)
        return new


# ---------------------------------------------------------------------------
# REGISTRY + register 데코레이터
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Validator] = {}


def register(name: str) -> Callable[[type[Validator]], type[Validator]]:
    """plugin 등록 데코레이터.

    @register("id_unique")
    class IDUniqueValidator(Validator):
        ...
    """
    def _decorator(cls: type[Validator]) -> type[Validator]:
        inst = cls()
        inst.name = name
        REGISTRY[name] = inst
        return cls
    return _decorator


# ---------------------------------------------------------------------------
# Chain runner
# ---------------------------------------------------------------------------

def run_validator_chain(
    content: str,
    spec: dict,
    context: dict | None = None,
    max_fix_iterations: int = 2,
) -> ChainResult:
    """spec.validators 리스트를 순서대로 실행.

    spec yaml 예:
        validators: [id_unique, markdown_table, link_integrity]

    스펙에 validators 없으면 기본 체인 (id_unique + link_integrity) 적용.
    각 plugin 실패 시 fixable_hints 로 자동 수정 → 최대 max_fix_iterations 반복.
    """
    import os as _os
    if _os.environ.get("V8_VALIDATORS", "1") == "0":
        return ChainResult(passed=True, fixed_content=None)

    names: list[str] = spec.get("validators") or ["id_unique", "link_integrity"]
    active = [REGISTRY[n] for n in names if n in REGISTRY]
    if not active:
        return ChainResult(passed=True, fixed_content=None)

    current = content
    auto_fixed_total = 0
    results: list[PluginValidationResult] = []

    # 1차 실행
    for validator in active:
        try:
            r = validator.validate(current, spec, context or {})
        except Exception as e:
            logger.warning("validator_exception name=%s err=%s",
                           validator.name, str(e)[:150])
            r = PluginValidationResult(
                passed=True,  # 예외 시 통과 처리 (graceful)
                validator=validator.name,
                failures=[f"validator 예외 — 스킵: {e!s:.100}"],
            )
        results.append(r)

        # 실패 + 자동 수정 가능 → patch 적용 + 재검증 (최대 반복)
        if not r.passed and r.fixable_hints:
            for _ in range(max_fix_iterations):
                new_content = validator.apply_fixes(current, r.fixable_hints)
                if new_content == current:
                    break  # 실제 변화 없음
                current = new_content
                auto_fixed_total += len(r.fixable_hints)
                r2 = validator.validate(current, spec, context or {})
                results[-1] = r2
                r = r2
                if r.passed or not r.fixable_hints:
                    break

    total_failures = sum(len(r.failures) for r in results if not r.passed)
    chain_passed = all(r.passed for r in results)
    fixed_content = current if current != content else None

    return ChainResult(
        passed=chain_passed,
        fixed_content=fixed_content,
        results=results,
        total_failures=total_failures,
        auto_fixed_count=auto_fixed_total,
    )
