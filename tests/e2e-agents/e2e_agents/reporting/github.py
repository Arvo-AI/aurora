import subprocess
import sys

from e2e_agents.config.settings import Settings


COMMENT_MARKER = "<!-- e2e-agent-results -->"


def post_pr_comment(body: str, settings: Settings) -> bool:
    """Post or update the e2e agent results comment on a PR.

    Uses gh CLI (available natively in GitHub Actions).
    Returns True if successful.
    """
    if not settings.pr_number or not settings.repository:
        print("Cannot post PR comment: missing pr_number or repository", file=sys.stderr)
        return False

    marked_body = f"{COMMENT_MARKER}\n{body}"

    # Try to find and update existing comment first
    existing_id = _find_existing_comment(settings)
    if existing_id:
        return _update_comment(existing_id, marked_body, settings)

    # Create new comment
    return _create_comment(marked_body, settings)


def _find_existing_comment(settings: Settings) -> str | None:
    """Find the existing e2e agent comment by marker."""
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{settings.repository}/issues/{settings.pr_number}/comments",
                "--jq", f'.[] | select(.body | startswith("{COMMENT_MARKER}")) | .id',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Return the last matching comment ID
            return result.stdout.strip().split("\n")[-1]
    except Exception:
        pass
    return None


def _update_comment(comment_id: str, body: str, settings: Settings) -> bool:
    """Update an existing PR comment."""
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{settings.repository}/issues/comments/{comment_id}",
                "-X", "PATCH",
                "-f", f"body={body}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _create_comment(body: str, settings: Settings) -> bool:
    """Create a new PR comment."""
    try:
        result = subprocess.run(
            [
                "gh", "pr", "comment", str(settings.pr_number),
                "--repo", settings.repository,
                "--body", body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False
