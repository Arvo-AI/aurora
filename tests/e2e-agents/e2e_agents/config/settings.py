import os
from pathlib import Path
from pydantic_settings import BaseSettings


def _find_env_file() -> str | None:
    """Locate .env file — works both locally and when installed as a package."""
    candidates = [
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path.cwd() / "tests" / "e2e-agents" / ".env",
    ]
    # Also check relative to this file (only works in dev/editable installs)
    try:
        local = Path(__file__).resolve().parent.parent.parent.parent.parent / ".env"
        candidates.append(local)
    except Exception:
        # __file__ resolution can fail in some packaging contexts; fall back to cwd-based candidates.
        pass

    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except Exception:
            # Filesystem probe errors (e.g. permission denied on a candidate) are non-fatal.
            continue
    return None


def _default_results_dir() -> str:
    """Results dir relative to cwd, not package install location."""
    # In CI, cwd is the repo root
    ci_path = Path.cwd() / "tests" / "e2e-agents" / "results"
    if ci_path.parent.exists():
        return str(ci_path)
    # Fallback: relative to cwd
    return str(Path.cwd() / "results")


class Settings(BaseSettings):
    # Target app
    base_url: str = "http://localhost:3000"
    test_email: str = "1@a.ca"
    test_password: str = "browsertest123"

    # Multiple test users for parallel execution (JSON list)
    # Format: [{"email": "user1@test.com", "password": "pass1"}, ...]
    test_users: list[dict[str, str]] | None = None

    # LLM
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    model_timeout: float = 120.0

    # Browser
    headless: bool = True
    viewport_width: int = 1440
    viewport_height: int = 900

    # Execution limits
    max_total_steps: int = 200
    max_agents_parallel: int = 1

    # CI integration
    ci: bool = False
    github_token: str | None = None
    pr_number: int | None = None
    repository: str | None = None

    # Diff context — injected into prompts so agents know what changed
    diff_context: str | None = None

    # PR description — natural language description of what was changed
    pr_description: str | None = None

    # Output (RESULTS_DIR env var overrides default)
    results_dir: str = os.environ.get("RESULTS_DIR", _default_results_dir())

    # Labels (JSON list from CI, or comma-separated from CLI)
    labels: str = ""

    model_config = {
        "env_file": _find_env_file(),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
