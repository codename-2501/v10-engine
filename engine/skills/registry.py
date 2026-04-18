"""
Skill Registry — resolves node names to YAML skill specs.

Spec files live under ``specs/{phase}/`` and follow a naming convention
derived from the node name (spaces to underscores, prefixes stripped).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml


class SkillRegistry:
    """Finds, loads, and manages YAML skill specifications."""

    def __init__(self, specs_dir: Optional[Path] = None) -> None:
        """Initialise the registry.

        Args:
            specs_dir: Override for the specs root directory.
                       Defaults to ``<this-package>/specs``.
        """
        self.specs_dir: Path = specs_dir or Path(__file__).parent / "specs"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, node_name: str, phase: str, node_type: str) -> Optional[Dict]:
        """Resolve a node to its skill spec.

        Lookup order:
          1. ``specs/{phase}/{filename}.yaml``
          2. ``specs/_common.yaml``

        Args:
            node_name: Human-readable node name (e.g. ``"[QA] 기능명세서 (v2)"``).
            phase:     Phase string such as ``"PLANNING"`` or ``"DESIGN"``.
            node_type: ``"TASK"`` or ``"QA"``.

        Returns:
            Parsed YAML dict, or ``None`` if no spec is found.
        """
        filename = self._to_filename(node_name)
        phase_lower = phase.lower()

        # Primary: phase-specific spec
        primary = self.specs_dir / phase_lower / f"{filename}.yaml"
        if primary.is_file():
            return self._load(primary)

        # Fallback: common spec
        common = self.specs_dir / "_common.yaml"
        if common.is_file():
            return self._load(common)

        return None

    def list_all(self) -> List[Dict]:
        """List every spec file with basic metadata.

        Returns:
            A list of dicts, each containing ``phase``, ``name``, and
            ``path`` keys.
        """
        results: List[Dict] = []
        if not self.specs_dir.is_dir():
            return results

        for yaml_path in sorted(self.specs_dir.rglob("*.yaml")):
            rel = yaml_path.relative_to(self.specs_dir)
            parts = rel.parts
            phase = parts[0] if len(parts) > 1 else "_common"
            results.append({
                "phase": phase,
                "name": yaml_path.stem,
                "path": str(yaml_path),
            })
        return results

    def get_spec(self, phase: str, name: str) -> Optional[Dict]:
        """Load a specific spec by phase and name.

        Args:
            phase: Phase directory name (e.g. ``"planning"``).
            name:  Spec filename without extension.

        Returns:
            Parsed YAML dict, or ``None``.
        """
        path = self.specs_dir / phase.lower() / f"{name}.yaml"
        if path.is_file():
            return self._load(path)
        return None

    def save_spec(self, phase: str, name: str, data: Dict) -> None:
        """Save or update a spec file.

        Creates the phase directory if it does not exist.

        Args:
            phase: Phase directory name.
            name:  Spec filename without extension.
            data:  Dict to serialise as YAML.
        """
        phase_dir = self.specs_dir / phase.lower()
        phase_dir.mkdir(parents=True, exist_ok=True)
        path = phase_dir / f"{name}.yaml"
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh, allow_unicode=True, default_flow_style=False)

    def delete_spec(self, phase: str, name: str) -> bool:
        """Delete a spec file.

        Args:
            phase: Phase directory name.
            name:  Spec filename without extension.

        Returns:
            ``True`` if the file existed and was deleted.
        """
        path = self.specs_dir / phase.lower() / f"{name}.yaml"
        if path.is_file():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_filename(node_name: str) -> str:
        """Convert a human-readable node name to a spec filename stem.

        Transformations applied:
          * Strip leading ``[QA]`` prefix (with optional whitespace).
          * Remove any parenthetical suffix, e.g. ``(v2)`` or ``(초안)``.
          * Replace whitespace runs with underscores.
          * Lower-case the result.
          * Strip leading/trailing underscores.
        """
        name = node_name.strip()
        # Remove [QA] prefix
        name = re.sub(r"^\[QA\]\s*", "", name, flags=re.IGNORECASE)
        # Remove all parenthetical content
        name = re.sub(r"\s*\(.*?\)\s*", " ", name)
        # Special chars → underscore (파일시스템 안전)
        name = name.replace("/", "_").replace("·", "_")
        # Whitespace → underscores, lowercase
        name = re.sub(r"\s+", "_", name).lower()
        # 연속 언더스코어 정리
        name = re.sub(r"_+", "_", name)
        return name.strip("_")

    @staticmethod
    def _load(path: Path) -> Dict:
        """Load and parse a YAML file.

        Args:
            path: Absolute or relative path to the YAML file.

        Returns:
            Parsed dict.
        """
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
