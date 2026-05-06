"""CLI entry point for local development and manual testing."""
import asyncio
import sys

import click

from e2e_agents.agents.registry import get_agent, get_all_agents, resolve_agents_for_labels
from e2e_agents.config.settings import Settings
from e2e_agents.infra.wait_for_ready import wait_for_app
from e2e_agents.reporting.formatter import format_terminal_output
from e2e_agents.reporting.json_output import write_json_results
from e2e_agents.runner.engine import run_agents


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
def run(area, run_all, headless, model, max_steps, base_url, skip_health_check):
    """Run agent(s) against the target app."""
    settings = Settings(headless=headless)

    if model:
        settings.model = model
    if base_url:
        settings.base_url = base_url

    if not settings.anthropic_api_key:
        click.echo("Error: ANTHROPIC_API_KEY not set", err=True)
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
        click.echo("Specify --area or --all", err=True)
        sys.exit(1)

    if max_steps:
        definitions = [d.model_copy(update={"max_steps": max_steps}) for d in definitions]

    # Health check
    if not skip_health_check:
        click.echo(f"Checking {settings.base_url}...")
        if not wait_for_app(settings.base_url, timeout=30):
            click.echo(f"App not responding at {settings.base_url}. Use --skip-health-check to bypass.", err=True)
            sys.exit(1)

    click.echo(f"Running {len(definitions)} agent(s): {', '.join(d.name for d in definitions)}")
    click.echo(f"Model: {settings.model} | Headless: {headless}")
    click.echo("-" * 60)

    results = asyncio.run(run_agents(definitions, settings))

    # Output
    click.echo(format_terminal_output(results))

    # Save JSON
    output_path = write_json_results(results, settings.results_dir)
    click.echo(f"\nResults saved to: {output_path}")

    # Exit code
    if any(r.status in ("errored", "crashed") for r in results):
        sys.exit(2)
    sys.exit(0)


@cli.command("list")
def list_agents():
    """List all available agent definitions."""
    agents = get_all_agents()
    click.echo(f"Available agents ({len(agents)}):\n")
    for area, agent_def in sorted(agents.items()):
        click.echo(f"  {area:<20} {agent_def.name} (max {agent_def.max_steps} steps, {agent_def.timeout_seconds}s timeout)")


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
    click.echo(f"Timeout: {agent_def.timeout_seconds}s")
    click.echo("-" * 60)
    click.echo(agent_def.prompt_template)


if __name__ == "__main__":
    cli()
