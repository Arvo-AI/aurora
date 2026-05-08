import asyncio
import time
import traceback

from browser_use import Agent, ChatAnthropic

from e2e_agents.agents.base import AgentDefinition
from e2e_agents.config.settings import Settings
from e2e_agents.runner.browser import create_browser_session
from e2e_agents.runner.models import RunResult


async def run_agent(definition: AgentDefinition, settings: Settings) -> RunResult:
    """Execute a single agent and return structured results.

    Handles timeouts, browser crashes, and retries (once on crash).
    """
    start = time.time()
    browser_session = create_browser_session(settings)

    llm = ChatAnthropic(
        model=settings.model,
        api_key=settings.anthropic_api_key,
        timeout=settings.model_timeout,
    )

    prompt = definition.render_prompt(
        base_url=settings.base_url,
        email=settings.test_email,
        password=settings.test_password,
    )

    agent = Agent(
        task=prompt,
        llm=llm,
        browser_session=browser_session,
        use_vision=definition.use_vision,
        max_failures=definition.max_failures,
    )

    try:
        history = await asyncio.wait_for(
            agent.run(max_steps=definition.max_steps),
            timeout=definition.timeout_seconds,
        )

        elapsed = time.time() - start
        final = history.final_result() or ""
        errors = [str(e) for e in history.errors() if e is not None]

        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="completed",
            steps_used=len(history.history),
            max_steps=definition.max_steps,
            duration_seconds=elapsed,
            raw_findings=final,
            errors=errors,
            model_used=settings.model,
        )

    except asyncio.TimeoutError:
        elapsed = time.time() - start
        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="timed_out",
            steps_used=definition.max_steps,
            max_steps=definition.max_steps,
            duration_seconds=elapsed,
            raw_findings="Agent timed out before completing all tests.",
            errors=[f"Timed out after {definition.timeout_seconds}s"],
            model_used=settings.model,
        )

    except Exception as e:
        elapsed = time.time() - start
        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="crashed",
            duration_seconds=elapsed,
            max_steps=definition.max_steps,
            raw_findings="",
            errors=[f"{type(e).__name__}: {e}", traceback.format_exc()],
            model_used=settings.model,
        )

    finally:
        try:
            await browser_session.stop()
        except Exception:
            pass


async def run_agents(
    definitions: list[AgentDefinition],
    settings: Settings,
) -> list[RunResult]:
    """Run multiple agents sequentially, respecting total step budget."""
    results: list[RunResult] = []
    total_steps_used = 0

    for definition in definitions:
        remaining_budget = settings.max_total_steps - total_steps_used
        if remaining_budget <= 0:
            results.append(RunResult(
                agent_name=definition.name,
                area=definition.area,
                status="errored",
                max_steps=definition.max_steps,
                errors=["Skipped: total step budget exhausted"],
                model_used=settings.model,
            ))
            continue

        # Cap this agent's steps to remaining budget
        capped_def = definition.model_copy(
            update={"max_steps": min(definition.max_steps, remaining_budget)}
        )

        result = await run_agent(capped_def, settings)
        results.append(result)
        total_steps_used += result.steps_used

    return results
