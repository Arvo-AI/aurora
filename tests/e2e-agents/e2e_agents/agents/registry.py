"""
Agent registry — auto-discovers agent definitions and maps PR labels to them.

To add a new agent: create a new .py file in this directory with an AGENT_DEF export.
It will be discovered automatically.
"""
import importlib
import pkgutil
from pathlib import Path

from .base import AgentDefinition

_AGENTS: dict[str, AgentDefinition] | None = None

# Explicit label aliases (maps labels that don't match an area directly)
LABEL_ALIASES = {
    "area:frontend": "general",
    "area:ui": "general",
    "area:rca": "area:incidents",
}


def _discover_agents() -> dict[str, AgentDefinition]:
    """Scan this package for modules exporting AGENT_DEF."""
    agents = {}
    package_dir = Path(__file__).parent

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.name in ("__init__", "base", "registry"):
            continue
        try:
            module = importlib.import_module(f".{module_info.name}", package=__package__)
            if hasattr(module, "AGENT_DEF"):
                agent_def = module.AGENT_DEF
                agents[agent_def.area] = agent_def
        except Exception:
            continue

    return agents


def get_all_agents() -> dict[str, AgentDefinition]:
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = _discover_agents()
    return _AGENTS


def resolve_agents_for_labels(labels: list[str]) -> list[AgentDefinition]:
    """Given a list of PR labels, return the agent definitions to run."""
    all_agents = get_all_agents()
    resolved: dict[str, AgentDefinition] = {}

    for label in labels:
        # Direct match
        if label in all_agents:
            resolved[label] = all_agents[label]
            continue

        # Alias match
        target = LABEL_ALIASES.get(label)
        if target and target in all_agents:
            resolved[target] = all_agents[target]

    # Sort by priority (lower = runs first)
    return sorted(resolved.values(), key=lambda a: a.priority)


def get_agent(area: str) -> AgentDefinition | None:
    """Get a specific agent by area key."""
    all_agents = get_all_agents()
    return all_agents.get(area) or all_agents.get(f"area:{area}")
