import os
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

    env = _build_env(settings)

    # Try to find and update existing comment first
    existing_id = _find_existing_comment(settings, env)
    if existing_id:
        return _update_comment(existing_id, marked_body, settings, env)

    # Create new comment
    return _create_comment(marked_body, settings, env)


def _build_env(settings: Settings) -> dict[str, str]:
    """Build environment dict for gh CLI subprocess.

    Ensures GH_TOKEN is set so gh can authenticate.
    """
    env = os.environ.copy()
    if settings.github_token:
        env["GH_TOKEN"] = settings.github_token
    return env


def _find_existing_comment(settings: Settings, env: dict) -> str | None:
    """Find the existing e2e agent comment by marker.

    Handles pagination by requesting up to 100 comments.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{settings.repository}/issues/{settings.pr_number}/comments",
                "--paginate",
                "--jq",
                '.[] | select(.body | startswith("<!-- e2e-agent-results -->")) | .id',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Return the last matching comment ID
            return result.stdout.strip().split("\n")[-1]
    except Exception as e:
        print(f"Warning: Failed to find existing comment: {e}", file=sys.stderr)
    return None


def _update_comment(comment_id: str, body: str, settings: Settings, env: dict) -> bool:
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
            env=env,
        )
        if result.returncode != 0:
            print(f"Warning: Failed to update comment: {result.stderr}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"Warning: Exception updating comment: {e}", file=sys.stderr)
        return False


def _create_comment(body: str, settings: Settings, env: dict) -> bool:
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
            env=env,
        )
        if result.returncode != 0:
            print(f"Warning: Failed to create comment: {result.stderr}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"Warning: Exception creating comment: {e}", file=sys.stderr)
        return False
