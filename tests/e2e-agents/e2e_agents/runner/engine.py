import asyncio
import re
import time
import traceback
from pathlib import Path

from browser_use import Agent, ChatAnthropic

from e2e_agents.agents.base import AgentDefinition
from e2e_agents.config.settings import Settings
from e2e_agents.runner.browser import create_browser_session
from e2e_agents.runner.models import Issue, RunResult


def _estimate_timeout(max_steps: int) -> int:
    """Each step takes ~20-25s with vision. Add buffer for slow pages."""
    return max(max_steps * 25 + 60, 600)


async def _safe_stop_browser(browser_session, timeout: float = 10.0):
    """Stop browser session with a timeout to prevent hanging."""
    try:
        await asyncio.wait_for(browser_session.stop(), timeout=timeout)
    except asyncio.TimeoutError:
        # Best-effort shutdown: a timeout during cleanup should not fail the run.
        pass
    except Exception as exc:
        print(f"Warning: failed to stop browser session cleanly: {exc}")


# Each quantifier is bounded or separated by a required literal/digit, so the
# regex runs in linear time (avoids the polynomial backtracking SonarQube S5852
# flagged on the previous version).
_BUG_HEADER_RE = re.compile(
    r"(?:^|\n)[ \t]{0,8}(?:#{1,3}[ \t]+)?(?:\*\*)?"
    r"(?:(?:bug|issue)[ \t]+)?#?\d{1,8}[.): \t]",
    re.IGNORECASE,
)


_BLOCK_SKIP_WORDS = ("recommend", "status", "what works", "testing status", "fix needed", "urgent fix")
_LINE_SKIP_WORDS = ("✅", "passed", "works", "success", "verdict", "no bug", "no issue", "recommend")
_BUG_KEYWORDS = ("bug", "broken", "fail", "crash", "error", "missing", "stuck", "empty", "undefined")


def _is_bug_line(lower: str, line: str) -> Issue | None:
    """Check if a line looks like a standalone bug report and return an Issue if so."""
    if any(skip in lower for skip in _LINE_SKIP_WORDS):
        return None
    severity = _detect_severity(line)
    url = _extract_url(line)
    if severity and url and any(w in lower for w in _BUG_KEYWORDS):
        return Issue(page_url=url, description=_clean_description(line), severity=severity)
    return None


def _extract_issues_from_findings(raw_findings: str) -> list[Issue]:
    """Parse the LLM's raw output into structured Issue objects."""
    if not raw_findings:
        return []

    # Pattern 1: Structured bug reports (compiled regex handles BUG #1:, ### 1., **1., etc.)
    bug_blocks = _BUG_HEADER_RE.split(raw_findings)
    issues: list[Issue] = []

    for block in bug_blocks[1:]:
        first_line = block.strip().split("\n")[0].lower()
        if any(skip in first_line for skip in _BLOCK_SKIP_WORDS):
            continue
        issue = _parse_bug_block(block)
        if issue:
            issues.append(issue)

    if issues:
        return issues

    # Fallback: scan individual lines for bug-like statements with URLs
    for line in raw_findings.split("\n"):
        line = line.strip()
        if not line or len(line) < 20:
            continue
        issue = _is_bug_line(line.lower(), line)
        if issue:
            issues.append(issue)

    return issues


_URL_LABELS = ("url:", "location:", "page:", "**url**")
_DESC_LABELS = ("description:", "issue:", "**issue**:")


def _classify_line(line_lower: str) -> str:
    """Classify a bug block line as 'url', 'severity', 'description', or ''."""
    if any(k in line_lower for k in _URL_LABELS):
        return "url"
    if "severity:" in line_lower or "(high)" in line_lower or "(critical)" in line_lower or "(medium)" in line_lower:
        return "severity"
    if any(k in line_lower for k in _DESC_LABELS):
        return "description"
    return ""


def _parse_bug_block(block: str) -> Issue | None:
    """Parse a single bug block into an Issue."""
    lines = block.strip().split("\n")
    if not lines:
        return None

    description = ""
    url = "unknown"
    severity = "medium"
    has_label = False

    for line in lines[:15]:
        kind = _classify_line(line.lower().strip())
        if kind == "url":
            url = _extract_url(line) or url
            has_label = True
        elif kind == "severity":
            severity = _detect_severity(line) or severity
            has_label = True
        elif kind == "description":
            description = line.split(":", 1)[-1].strip()
            has_label = True

    # Blocks without any labeled content (url/severity/description) are likely
    # not bug reports (e.g. recommendation items).
    if not has_label and len(lines) < 3:
        return None

    if url == "unknown":
        url = next((_extract_url(l) for l in lines[:15] if _extract_url(l)), "unknown")

    if severity == "medium":
        severity = _detect_severity(lines[0]) or severity

    if not description:
        description = _clean_description(lines[0])

    if not description or len(description) < 5:
        return None

    return Issue(page_url=url, description=description, severity=severity)


def _detect_severity(text: str) -> str | None:
    text_lower = text.lower()
    if "critical" in text_lower or "catastrophic" in text_lower:
        return "critical"
    if "high" in text_lower:
        return "high"
    if "medium" in text_lower or "moderate" in text_lower:
        return "medium"
    if "low" in text_lower or "minor" in text_lower:
        return "low"
    if any(w in text_lower for w in ["crash", "broken", "fails", "non-functional", "black screen"]):
        return "high"
    if any(w in text_lower for w in ["slow", "loading", "intermittent", "undefined"]):
        return "medium"
    return None


_RE_LOCALHOST_URL = re.compile(r"(https?://localhost[^\s,)\"']{1,200})")
_RE_PATH = re.compile(r"(/[a-z][a-z0-9/_-]{1,200})")
_URL_FALSE_POSITIVES = ("/null", "/undefined", "/object")


def _extract_url(text: str) -> str | None:
    match = _RE_LOCALHOST_URL.search(text)
    if match:
        return match.group(1)
    match = _RE_PATH.search(text)
    if match:
        url = match.group(1)
        if any(fp in url for fp in _URL_FALSE_POSITIVES):
            return None
        return f"http://localhost:3000{url}"
    return None


_RE_LEADING_BULLET = re.compile(r"^\s{0,4}[-*•]\s{0,2}")
_RE_BOLD = re.compile(r"\*\*([^*]{1,200})\*\*")
_RE_ITALIC = re.compile(r"\*([^*]{1,200})\*")


def _clean_description(text: str) -> str:
    text = _RE_LEADING_BULLET.sub("", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    return text.strip()[:300]


async def _run_single_attempt(
    definition: AgentDefinition,
    settings: Settings,
    timeout: int,
) -> RunResult:
    """Execute a single agent attempt."""
    start = time.time()
    browser_session = create_browser_session(settings)
    steps_completed = 0

    llm = ChatAnthropic(
        model=settings.model,
        api_key=settings.anthropic_api_key,
        timeout=settings.model_timeout,
    )

    prompt = definition.render_prompt(
        base_url=settings.base_url,
        email=settings.test_email,
        password=settings.test_password,
        pr_description=settings.pr_description,
    )

    # Inject diff context if available
    if settings.diff_context:
        prompt += (
            "\n\nCONTEXT: These files were changed in this PR:\n"
            f"{settings.diff_context}\n"
            "Pay extra attention to the UI areas affected by these changes."
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
            timeout=timeout,
        )

        elapsed = time.time() - start
        final = history.final_result() or ""
        errors = [str(e) for e in history.errors() if e is not None]
        steps_completed = len(history.history)

        issues = _extract_issues_from_findings(final)

        # Capture final screenshot
        screenshots: list[str] = []
        try:
            screenshot_dir = Path(settings.results_dir) / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = screenshot_dir / f"{definition.area.replace(':', '_')}_final.png"
            page = await browser_session.get_current_page()
            if page:
                await page.screenshot(path=str(screenshot_path))
                screenshots.append(str(screenshot_path))
        except Exception as e:
            errors.append(f"Final screenshot capture failed: {e}")

        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="completed",
            issues=issues,
            steps_used=steps_completed,
            max_steps=definition.max_steps,
            duration_seconds=elapsed,
            raw_findings=final,
            errors=errors,
            model_used=settings.model,
            screenshots=screenshots,
        )

    except asyncio.TimeoutError:
        elapsed = time.time() - start
        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="timed_out",
            steps_used=steps_completed if steps_completed > 0 else definition.max_steps,
            max_steps=definition.max_steps,
            duration_seconds=elapsed,
            raw_findings="Agent timed out before completing all tests.",
            errors=[f"Timed out after {timeout}s"],
            model_used=settings.model,
        )

    finally:
        await _safe_stop_browser(browser_session)


async def run_agent(definition: AgentDefinition, settings: Settings) -> RunResult:
    """Execute an agent with retry on crash.

    Retries once with a fresh browser session if the first attempt crashes.
    """
    timeout = definition.timeout_seconds or _estimate_timeout(definition.max_steps)

    try:
        result = await _run_single_attempt(definition, settings, timeout)
        if result.status != "crashed":
            return result
    except Exception as e:
        result = RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="crashed",
            max_steps=definition.max_steps,
            errors=[f"{type(e).__name__}: {e}", traceback.format_exc()],
            model_used=settings.model,
        )

    # Retry once on crash
    try:
        retry_result = await _run_single_attempt(definition, settings, timeout)
        retry_result.retried = True
        return retry_result
    except Exception as e:
        return RunResult(
            agent_name=definition.name,
            area=definition.area,
            status="crashed",
            max_steps=definition.max_steps,
            duration_seconds=result.duration_seconds,
            errors=result.errors + [f"Retry also failed: {type(e).__name__}: {e}"],
            model_used=settings.model,
            retried=True,
        )


async def run_agents(
    definitions: list[AgentDefinition],
    settings: Settings,
) -> list[RunResult]:
    """Run multiple agents, respecting total step budget and parallelism."""
    results: list[RunResult] = []
    total_steps_used = 0

    if settings.max_agents_parallel > 1 and len(definitions) > 1:
        # Parallel execution with concurrency cap and separate test users
        semaphore = asyncio.Semaphore(settings.max_agents_parallel)

        async def _bounded_run(i: int, definition: AgentDefinition) -> RunResult:
            async with semaphore:
                agent_settings = _get_isolated_settings(settings, i)
                return await run_agent(definition, agent_settings)

        tasks = [_bounded_run(i, d) for i, d in enumerate(definitions)]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for item in gathered:
            if isinstance(item, Exception):
                results.append(RunResult(
                    agent_name="unknown",
                    area="unknown",
                    status="crashed",
                    errors=[f"{type(item).__name__}: {item}"],
                    model_used=settings.model,
                ))
            else:
                results.append(item)
        return results

    # Sequential execution (default)
    for definition in definitions:
        remaining_budget = settings.max_total_steps - total_steps_used
        if remaining_budget <= 5:  # Don't bother with <5 steps
            results.append(RunResult(
                agent_name=definition.name,
                area=definition.area,
                status="errored",
                max_steps=definition.max_steps,
                errors=["Skipped: total step budget exhausted"],
                model_used=settings.model,
            ))
            continue

        capped_def = definition.model_copy(
            update={"max_steps": min(definition.max_steps, remaining_budget)}
        )

        result = await run_agent(capped_def, settings)
        results.append(result)
        total_steps_used += result.steps_used

    return results


def _get_isolated_settings(settings: Settings, index: int) -> Settings:
    """Create isolated settings for parallel execution.

    Each agent gets a unique test user to prevent session conflicts.
    """
    if settings.test_users:
        users = settings.test_users
        user = users[index % len(users)]
        return settings.model_copy(update={
            "test_email": user["email"],
            "test_password": user["password"],
        })
    return settings
