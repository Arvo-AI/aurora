"""
Adversarial Browser Use agent: Tries to break things, not validate them.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from browser_use import Agent, BrowserSession, ChatAnthropic

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(str(env_path), override=False)


async def run_adversarial_test():
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

    task = """You are a hostile QA tester. Your job is to FIND PROBLEMS, not confirm things work.
You get paid per bug found. If you report "everything works fine" you get fired.

Test Aurora at {base_url} (email: {email}, password: {password})

DO THESE THINGS and report what breaks:

1. Sign in, then immediately try to sign out and sign back in. Does session handling work?

2. Go to incidents. Try clicking rapidly on different incidents. Does anything break?

3. On an incident detail page:
   - Click the thoughts panel toggle rapidly
   - Try to copy text from the summary
   - Scroll to the very bottom - is anything cut off?
   - Check if the "Back to incidents" button works from deep in the page

4. Try navigating directly to a URL that doesn't exist (like /incidents/fake-id-12345)
   - Does it show a proper error page or does it crash?

5. Go to Settings. Try disconnecting a provider. What happens?

6. Open the browser console (use keyboard shortcut or just note if you see any red errors
   in the page that indicate JS errors)

7. Try resizing the browser window very small. Does the layout break?

8. Time each page load. Anything over 3 seconds is a bug.

9. Look for:
   - Any "undefined" or "null" text rendered on screen
   - Any empty states that shouldn't be empty
   - Broken images or icons
   - Text that overflows its container
   - Buttons that have no hover state or don't respond

REPORT FORMAT: List every single issue. Include the page URL and a description.
If you truly find nothing wrong (unlikely), explain exactly what you tested and why
you're confident there are no issues. Do NOT just say "everything works"."""

    base_url = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
    email = os.environ.get("E2E_EMAIL", os.environ.get("TEST_EMAIL", "1@a.ca"))
    password = os.environ.get("E2E_PASSWORD", os.environ.get("TEST_PASSWORD", ""))
    task = task.format(base_url=base_url, email=email, password=password)

    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser_session,
        use_vision=True,
        max_failures=5,
    )

    print("Starting ADVERSARIAL Browser Use agent...")
    print("-" * 60)

    try:
        history = await agent.run(max_steps=50)
        elapsed = time.time() - start

        final = history.final_result()

        print("\n" + "=" * 60)
        print("ADVERSARIAL TEST REPORT")
        print("=" * 60)
        print(f"Time: {elapsed:.1f}s | Steps: {len(history.history)}")
        print(f"\n{final}")
        print("=" * 60)

        report_path = Path(__file__).parent / "results" / "adversarial_test.json"
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps({
            "test": "adversarial_incidents",
            "time_seconds": elapsed,
            "steps": len(history.history),
            "findings": final,
            "errors": [str(e) for e in history.errors()],
        }, indent=2))
        print(f"\nSaved to: {report_path}")

    except Exception as e:
        print(f"\nAgent failed: {e}")
        raise
    finally:
        try:
            await asyncio.wait_for(browser_session.stop(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass  # Best-effort cleanup; don't mask the real error


if __name__ == "__main__":
    asyncio.run(run_adversarial_test())
