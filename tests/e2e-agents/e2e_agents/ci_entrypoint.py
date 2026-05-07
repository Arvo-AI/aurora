"""CI entry point — called by GitHub Actions workflow.

Reads PR labels from environment, resolves agents, runs them, posts results.
"""
import asyncio
import json
import os
import subprocess
import sys

from e2e_agents.agents.registry import resolve_agents_for_labels
from e2e_agents.config.settings import Settings
from e2e_agents.infra.wait_for_ready import wait_for_app
from e2e_agents.reporting.formatter import format_pr_comment, format_terminal_output
from e2e_agents.reporting.github import post_pr_comment
from e2e_agents.reporting.json_output import write_json_results
from e2e_agents.runner.engine import run_agents


def parse_labels() -> list[str]:
    """Parse PR labels from environment."""
    raw = os.environ.get("LABELS", "")
    if not raw:
        return []

    # Try JSON array first (from GitHub Actions toJSON)
    try:
        labels = json.loads(raw)
        if isinstance(labels, list):
            return [str(l) for l in labels]
    except (json.JSONDecodeError, TypeError):
        # Invalid or non-list JSON payload; fall through to comma-separated parsing.
        pass

    # Fall back to comma-separated
    return [l.strip() for l in raw.split(",") if l.strip()]


def parse_pr_number() -> int | None:
    """Safely parse PR number from environment."""
    raw = os.environ.get("PR_NUMBER", "").strip()
    if not raw:
        return None
    try:
        num = int(raw)
        return num if num > 0 else None
    except (ValueError, TypeError):
        return None


def get_pr_description(pr_number: int | None, repository: str | None) -> str | None:
    """Fetch PR description (body) from GitHub."""
    if not pr_number or not repository:
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repository, "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        body = result.stdout.strip()
        return body if body else None
    except Exception:
        return None


def get_diff_context() -> str | None:
    """Get the list of changed files in the PR for context injection.

    Uses git diff against the base branch to find changed frontend files.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None

        files = result.stdout.strip().split("\n")
        # Filter to frontend-relevant files
        relevant = [
            f for f in files
            if f.startswith("client/") and f.endswith((".tsx", ".ts", ".css"))
        ]

        if not relevant:
            return None

        # Limit to 30 files to keep prompt reasonable
        if len(relevant) > 30:
            relevant = relevant[:30] + [f"... and {len(relevant) - 30} more"]

        return "\n".join(relevant)

    except Exception:
        return None


async def main():
    pr_number = parse_pr_number()
    settings = Settings(
        ci=True,
        pr_number=pr_number,
        repository=os.environ.get("REPOSITORY"),
        github_token=os.environ.get("GITHUB_TOKEN"),
    )

    # Validate API key early
    if not settings.anthropic_api_key:
        print("Error: ANTHROPIC_API_KEY not set. Cannot run agents.", file=sys.stderr)
        sys.exit(2)

    labels = parse_labels()
    if not labels:
        print("No labels found in environment. Nothing to run.")
        return

    print(f"PR #{settings.pr_number} labels: {labels}")

    # Resolve agents
    agents_to_run = resolve_agents_for_labels(labels)
    if not agents_to_run:
        print(f"No matching agents for labels: {labels}")
        return

    # Fetch PR description early so we can filter agents that need it
    pr_desc = get_pr_description(pr_number, settings.repository)

    # Filter out agents that require a PR description if none available
    if not pr_desc:
        skipped = [a.name for a in agents_to_run if a.requires_pr_description]
        agents_to_run = [a for a in agents_to_run if not a.requires_pr_description]
        if skipped:
            print(f"Skipping agents (no PR description): {skipped}")
        if not agents_to_run:
            print("No agents to run after filtering.")
            return

    print(f"Agents to run: {[a.name for a in agents_to_run]}")

    # Inject PR description
    if pr_desc:
        settings.pr_description = pr_desc
        print(f"PR description injected ({len(pr_desc)} chars)")

    # Inject diff context
    diff = get_diff_context()
    if diff:
        settings.diff_context = diff
        print(f"Diff context: {len(diff.splitlines())} changed files injected into prompts")

    # Health check
    print(f"Waiting for app at {settings.base_url}...")
    if not wait_for_app(settings.base_url, timeout=180):
        print("App failed to become healthy within 180s", file=sys.stderr)
        comment = (
            "## 🔍 E2E Agent Test Results\n\n"
            "❌ **Infrastructure failure**: App did not become healthy within 180 seconds.\n"
            "Tests could not run."
        )
        post_pr_comment(comment, settings)
        sys.exit(2)

    print("App is ready. Starting agents...")

    # Run agents (wrapped to catch unexpected crashes)
    try:
        results = await run_agents(agents_to_run, settings)
    except Exception as e:
        print(f"Fatal error running agents: {e}", file=sys.stderr)
        comment = (
            "## 🔍 E2E Agent Test Results\n\n"
            f"💥 **Framework crash**: `{type(e).__name__}: {e}`\n"
            "Tests could not complete."
        )
        post_pr_comment(comment, settings)
        sys.exit(2)

    # Output to terminal
    print(format_terminal_output(results))

    # Save JSON artifact
    output_path = write_json_results(results, settings.results_dir)
    print(f"Results saved to: {output_path}")

    # Post PR comment
    comment = format_pr_comment(results, labels)
    if settings.pr_number:
        if post_pr_comment(comment, settings):
            print("Posted results to PR.")
        else:
            print("Failed to post PR comment.", file=sys.stderr)

    # Exit code: 2 for infra failures, 0 otherwise (advisory mode)
    if any(r.status in ("errored", "crashed") for r in results):
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
