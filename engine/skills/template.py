"""
Lightweight Mustache-style template engine for skill prompts.

Supports ``{{key}}`` and nested ``{{parent.child}}`` placeholders.
Unknown variables are left as-is so that downstream consumers can
detect or fill them later.
"""

from __future__ import annotations

import re
from typing import Any, Dict


def render(template: str, variables: Dict[str, Any]) -> str:
    """Render a template string by substituting ``{{key}}`` placeholders.

    * Flat keys:   ``{{name}}``          -> ``variables["name"]``
    * Nested keys: ``{{project.name}}``  -> ``variables["project"]["name"]``
    * Missing keys are left untouched (no ``KeyError``).

    Args:
        template:  The template string containing ``{{…}}`` placeholders.
        variables: A (possibly nested) dict of substitution values.

    Returns:
        The rendered string.
    """

    def _replace(match: re.Match) -> str:
        """Resolve a single placeholder."""
        key = match.group(1).strip()
        value = _resolve(key, variables)
        if value is None:
            # Unknown variable — return the original placeholder intact.
            return match.group(0)
        return str(value)

    return re.sub(r"\{\{(.+?)\}\}", _replace, template)


def _resolve(key: str, variables: Dict[str, Any]) -> Any:
    """Walk a dotted key path through *variables*.

    Args:
        key:       Dotted key such as ``"project.name"``.
        variables: Root dict to traverse.

    Returns:
        The resolved value, or ``None`` if any segment is missing.
    """
    parts = key.split(".")
    current: Any = variables
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
