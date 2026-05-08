"""CLI entry point for local development and manual testing."""
import asyncio
import subprocess
import sys

import click

from e2e_agents.agents.registry import get_agent, get_all_agents
from e2e_agents.config.settings import Settings
from e2e_agents.infra.wait_for_ready import wait_for_app
from e2e_agents.reporting.formatter import format_terminal_output
from e2e_agents.reporting.json_output import write_json_results
from e2e_agents.runner.engine import run_agents


def _get_local_diff_context() -> str | None:
    """Get changed files on current branch vs main for local context injection."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        files = result.stdout.strip().split("\n")
        relevant = [
            f for f in files
            if f.startswith("client/") and f.endswith((".tsx", ".ts", ".css"))
        ]
        return "\n".join(relevant) if relevant else None
    except Exception:
        return None


@click.group()
def cli():
    """Aurora E2E Agent Testing Framework"""


@cli.command()
@click.option("--area", "-a", multiple=True, help="Agent area(s) to run (e.g. incidents, settings)")
@click.option("--all", "run_all", is_flag=True, help="Run all agents")
@click.option("--headless/--no-headless", default=True, help="Run browser headlessly")
@click.option("--model", default=None, help="Override LLM model")
@click.option("--max-steps", type=int, default=None, help="Override max steps per agent")
@click.option("--base-url", default=None, help="Override target URL")
@click.option("--skip-health-check", is_flag=True, help="Skip waiting for app to be ready")
@click.option("--diff-context/--no-diff-context", default=True, help="Inject git diff into prompts")
@click.option("--parallel", type=int, default=1, help="Number of agents to run in parallel")
def run(area, run_all, headless, model, max_steps, base_url, skip_health_check, diff_context, parallel):
    """Run agent(s) against the target app."""
    settings = Settings(headless=headless, max_agents_parallel=parallel)

    if model:
        settings.model = model
    if base_url:
        settings.base_url = base_url

    if not settings.anthropic_api_key:
        click.echo("Error: ANTHROPIC_API_KEY not set. Set it in .env or export it.", err=True)
        sys.exit(1)

    # Resolve which agents to run
    if run_all:
        definitions = list(get_all_agents().values())
    elif area:
        definitions = []
        for a in area:
            agent_def = get_agent(a)
            if agent_def:
                definitions.append(agent_def)
            else:
                click.echo(f"Warning: no agent found for area '{a}'", err=True)
        if not definitions:
            click.echo("No valid agents to run.", err=True)
            sys.exit(1)
    else:
        click.echo("Specify --area or --all. Use `e2e-agents list` to see available agents.", err=True)
        sys.exit(1)

    if max_steps:
        definitions = [d.model_copy(update={"max_steps": max_steps}) for d in definitions]

    # Diff context
    if diff_context:
        diff = _get_local_diff_context()
        if diff:
            settings.diff_context = diff
            click.echo(f"Injecting diff context: {len(diff.splitlines())} changed files")

    # Health check
    if not skip_health_check:
        click.echo(f"Checking {settings.base_url}...")
        if not wait_for_app(settings.base_url, timeout=30):
            click.echo(f"App not responding at {settings.base_url}. Use --skip-health-check to bypass.", err=True)
            sys.exit(1)

    click.echo(f"Running {len(definitions)} agent(s): {', '.join(d.name for d in definitions)}")
    click.echo(f"Model: {settings.model} | Headless: {headless} | Parallel: {parallel}")
    click.echo("-" * 60)

    results = asyncio.run(run_agents(definitions, settings))

    # Output
    click.echo(format_terminal_output(results))

    # Save JSON
    output_path = write_json_results(results, settings.results_dir)
    click.echo(f"\nResults saved to: {output_path}")

    # Summary
    total_issues = sum(len(r.issues) for r in results)
    if total_issues > 0:
        click.echo(f"\n🐛 {total_issues} structured issue(s) extracted from findings.")

    # Exit code
    if any(r.status in ("errored", "crashed") for r in results):
        sys.exit(2)
    sys.exit(0)


@cli.command("list")
def list_agents():
    """List all available agent definitions."""
    agents = get_all_agents()
    if not agents:
        click.echo("No agents found. Check that agent modules are properly installed.")
        return

    click.echo(f"Available agents ({len(agents)}):\n")
    for area, agent_def in sorted(agents.items()):
        timeout = agent_def.timeout_seconds or f"~{agent_def.max_steps * 25 + 60}s (auto)"
        click.echo(f"  {area:<20} {agent_def.name} (max {agent_def.max_steps} steps, {timeout})")


@cli.command("show")
@click.argument("area")
def show_prompt(area):
    """Show the prompt template for a given area."""
    agent_def = get_agent(area)
    if not agent_def:
        click.echo(f"No agent found for area '{area}'", err=True)
        sys.exit(1)

    click.echo(f"Agent: {agent_def.name}")
    click.echo(f"Area: {agent_def.area}")
    click.echo(f"Max steps: {agent_def.max_steps}")
    timeout = agent_def.timeout_seconds or f"~{agent_def.max_steps * 25 + 60}s (auto)"
    click.echo(f"Timeout: {timeout}")
    click.echo("-" * 60)
    click.echo(agent_def.prompt_template)


if __name__ == "__main__":
    cli()
