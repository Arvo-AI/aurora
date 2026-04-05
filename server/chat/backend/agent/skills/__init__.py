"""
Skills-based prompt system for the Aurora agent.

Extracts integration knowledge from monolithic prompt builders into
self-contained markdown files loaded on-demand to reduce context rot
and optimize token usage.
"""

from .registry import SkillRegistry
from .load_skill_tool import load_skill, LoadSkillArgs

__all__ = ["SkillRegistry", "load_skill", "LoadSkillArgs"]
