"""
Agent tool: get_connected_repos
Returns all GitHub repos the user has connected, with metadata summaries.
The agent uses this to decide which repo(s) to investigate during RCA.
"""
from pydantic import BaseModel


class GetConnectedReposArgs(BaseModel):
    """No required args -- reads from user context."""
    pass


def get_connected_repos(**kwargs) -> str:
    """Return connected GitHub repositories with their descriptions."""
    from .vcs_rca_utils import get_connected_repos_for_provider
    return get_connected_repos_for_provider(kwargs.get("user_id"), "github")
