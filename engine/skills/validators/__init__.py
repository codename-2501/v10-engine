"""
Programmatic validators for skill-based artifact checking.

Validators inspect LLM output against rules defined in YAML skill specs,
catching structural and content issues *without* an additional API call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from engine.skills.validators.structure import StructureValidator
from engine.skills.validators.content import ContentValidator


@dataclass
class ValidationResult:
    """Outcome of running all validators against an artifact."""

    passed: bool
    issues: List[str]


def validate(content: str, spec: dict) -> ValidationResult:
    """Run all programmatic validators against *content* using *spec* rules.

    Args:
        content: The artifact text to validate.
        spec:    The skill spec dict (must contain a ``validation`` section
                 with a ``structural`` sub-dict for any checks to run).

    Returns:
        A ``ValidationResult`` indicating pass/fail and any issues found.
    """
    issues: List[str] = []
    validation = spec.get("validation", {})
    structural = validation.get("structural", {})

    if structural:
        issues.extend(StructureValidator.validate(content, structural))
        issues.extend(ContentValidator.validate(content, structural))

    return ValidationResult(passed=len(issues) == 0, issues=issues)
