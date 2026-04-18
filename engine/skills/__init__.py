"""
E+ Skill Hybrid System.

Skill-based executor that integrates with DAGAdvancer and the 5-Layer
ContextAssembler without modifying any existing engine components.
"""

from engine.skills.registry import SkillRegistry
from engine.skills.template import render
from engine.skills.executor import create_skill_executor

__all__ = [
    "SkillRegistry",
    "render",
    "create_skill_executor",
]
