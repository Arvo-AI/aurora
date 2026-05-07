"""
Browser Use agent: Test the incidents flow in Aurora.
Spawns an LLM agent that signs in, navigates to incidents, and reports issues.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from pydantic import BaseModel

from browser_use import Agent, BrowserSession, ChatAnthropic

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(str(env_path), override=False)


class Issue(BaseModel):
    page: str
    description: str
    severity: str  # "critical", "high", "medium", "low"


class TestReport(BaseModel):
    issues: list[Issue]
    pages_visited: list[str]
    total_time_seconds: float
    summary: str


async def run_incidents_test():
    start = time.time()

    browser_session = BrowserSession(
        headless=False,
        viewport={"width": 1440, "height": 900},
        wait_for_network_idle_page_load_time=2.0,
        minimum_wait_page_load_time=1.0,
        disable_security=True,
    )

    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=120.0,
    )

    task = """You are a QA engineer testing Aurora, an incident management platform.

Your job: Sign in, navigate to the incidents page, and thoroughly test the incidents feature.
Report ANY issues you find — broken UI, slow loading, missing data, confusing UX, errors.

STEPS:
1. Go to {base_url}
2. You will see a login page. Sign in with:
   - Email: {email}
   - Password: {password}
3. After login, navigate to the Incidents page (look for it in the sidebar/navigation)
4. On the incidents list page, check:
   - Do incidents load? How long does it take?
   - Are there any error states visible?
   - Does the severity/status show correctly for each incident?
   - Try clicking on an incident to view its details
5. On the incident detail page, check:
   - Does the summary/analysis render?
   - Are citations visible?
   - Is the thoughts panel accessible?
   - Does the status badge show correctly?
   - Is there any "Analysis Error" shown?
6. Try navigating back to the list and opening a different incident
7. Check if the page is responsive — does anything look broken or misaligned?

IMPORTANT:
- If a page takes more than 5 seconds to load meaningful content, report it as an issue.
- If you see any error messages, red badges, or broken states, report them.
- If buttons don't respond to clicks, report it.
- Be honest — if everything works fine, say so. Don't invent problems.
- You have maximum 40 steps. Be efficient."""

    base_url = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
    email = os.environ.get("E2E_EMAIL", os.environ.get("TEST_EMAIL", "1@a.ca"))
    password = os.environ.get("E2E_PASSWORD", os.environ.get("TEST_PASSWORD", ""))
    task = task.format(base_url=base_url, email=email, password=password)

    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser_session,
        use_vision=True,
        max_failures=3,
    )

    print("Starting Browser Use agent for incidents testing...")
    print(f"Target: {base_url}")
    print("-" * 60)

    try:
        history = await agent.run(max_steps=40)

        elapsed = time.time() - start

        # Get final result
        final = history.final_result()
        is_done = history.is_done()

        print("\n" + "=" * 60)
        print("AGENT REPORT")
        print("=" * 60)
        print(f"Completed: {is_done}")
        print(f"Total time: {elapsed:.1f}s")
        print(f"Steps taken: {len(history.history)}")
        print(f"\nFindings:\n{final}")
        print("=" * 60)

        # Save full report
        report_path = Path(__file__).parent / "results" / "incidents_test.json"
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps({
            "test": "incidents",
            "completed": is_done,
            "time_seconds": elapsed,
            "steps": len(history.history),
            "findings": final,
            "errors": [str(e) for e in history.errors()],
        }, indent=2))
        print(f"\nFull report saved to: {report_path}")

    except Exception as e:
        print(f"\nAgent failed with error: {e}")
        raise
    finally:
        try:
            await asyncio.wait_for(browser_session.stop(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass  # Best-effort cleanup; don't mask the real error


if __name__ == "__main__":
    asyncio.run(run_incidents_test())
