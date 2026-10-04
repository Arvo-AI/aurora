"""Slack shim over ``utils.notifications.team_routing``.

The team-routing agent (prompt + dispatch) is platform-generic; this module
keeps the original Slack-only names and signatures so existing callers and
test patch targets are untouched. See ``team_routing`` for the contract.
"""

from typing import Any, Dict

from utils.notifications import team_routing as _tr

SLACK = _tr.SPECS["slack"]
_LOG_PREFIX = SLACK.log_prefix


def _recurrence_context(incident_data: Dict[str, Any]) -> str:
    return _tr._recurrence_context(incident_data, SLACK)


def _build_prompt(incident_data: Dict[str, Any], incident_index: str) -> str:
    return _tr._build_prompt(incident_data, incident_index, SLACK)


def trigger_team_routing_agent(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Dispatch the background team-routing agent for a concluded incident on
    Slack. See ``team_routing.trigger_team_routing_agent``."""
    return _tr.trigger_team_routing_agent(user_id, incident_data, platform="slack")
