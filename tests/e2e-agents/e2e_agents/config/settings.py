from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Target app
    base_url: str = "http://localhost:3000"
    test_email: str = "1@a.ca"
    test_password: str = "browsertest123"

    # LLM
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    model_timeout: float = 120.0

    # Browser
    headless: bool = True
    viewport_width: int = 1440
    viewport_height: int = 900

    # Execution limits
    max_total_steps: int = 150
    max_agents_parallel: int = 1

    # CI integration
    ci: bool = False
    github_token: str | None = None
    pr_number: int | None = None
    repository: str | None = None

    # Output
    results_dir: str = str(Path(__file__).parent.parent.parent / "results")

    # Labels (JSON list from CI, or comma-separated from CLI)
    labels: str = ""

    model_config = {
        "env_file": str(Path(__file__).parent.parent.parent.parent.parent / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
