"""CI entry point — called by GitHub Actions workflow.

Reads PR labels from environment, resolves agents, runs them, posts results.
"""
import asyncio
import json
import os
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
        pass

    # Fall back to comma-separated
    return [l.strip() for l in raw.split(",") if l.strip()]


async def main():
    settings = Settings(
        ci=True,
        pr_number=int(os.environ.get("PR_NUMBER", "0")) or None,
        repository=os.environ.get("REPOSITORY"),
        github_token=os.environ.get("GITHUB_TOKEN"),
    )

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

    print(f"Agents to run: {[a.name for a in agents_to_run]}")

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

    # Run agents
    results = await run_agents(agents_to_run, settings)

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
