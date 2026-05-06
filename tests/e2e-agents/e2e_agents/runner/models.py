from typing import Literal
from pydantic import BaseModel


class Issue(BaseModel):
    page_url: str
    description: str
    severity: Literal["critical", "high", "medium", "low"]
    screenshot_path: str | None = None


class RunResult(BaseModel):
    agent_name: str
    area: str
    status: Literal["completed", "timed_out", "errored", "crashed"]
    issues: list[Issue] = []
    pages_visited: list[str] = []
    steps_used: int = 0
    max_steps: int = 0
    duration_seconds: float = 0.0
    raw_findings: str = ""
    errors: list[str] = []
    model_used: str = ""
    retried: bool = False
    screenshots: list[str] = []
