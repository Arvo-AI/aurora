from e2e_agents.runner.models import RunResult


def _status_icon(status: str) -> str:
    return {
        "completed": "✅",
        "timed_out": "⏱️",
        "errored": "❌",
        "crashed": "💥",
    }.get(status, "❓")


def _truncate(text: str, max_len: int = 3000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n... (truncated)"


def format_pr_comment(results: list[RunResult], labels: list[str]) -> str:
    total_duration = sum(r.duration_seconds for r in results)
    total_steps = sum(r.steps_used for r in results)

    has_findings = any(r.raw_findings and r.status == "completed" for r in results)
    has_errors = any(r.status in ("errored", "crashed") for r in results)

    lines = [
        "## 🔍 E2E Agent Test Results",
        "",
        f"**Labels**: {', '.join(f'`{l}`' for l in labels)}",
        f"**Duration**: {total_duration:.0f}s | **Steps**: {total_steps}",
        "",
    ]

    for result in results:
        icon = _status_icon(result.status)
        lines.append(f"### {icon} {result.agent_name} (`{result.area}`)")
        lines.append(
            f"**Status**: {result.status} "
            f"({result.steps_used}/{result.max_steps} steps) | "
            f"⏱️ {result.duration_seconds:.0f}s"
        )
        lines.append("")

        if result.status == "completed" and result.raw_findings:
            lines.append("<details><summary>Agent findings</summary>")
            lines.append("")
            lines.append(_truncate(result.raw_findings))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        elif result.status in ("errored", "crashed"):
            lines.append(f"**Errors**: {'; '.join(result.errors[:3])}")
            lines.append("")
        elif result.status == "timed_out":
            lines.append(f"Agent timed out after {result.duration_seconds:.0f}s.")
            if result.raw_findings:
                lines.append("<details><summary>Partial findings</summary>")
                lines.append("")
                lines.append(_truncate(result.raw_findings))
                lines.append("")
                lines.append("</details>")
            lines.append("")

    lines.append("---")
    lines.append(
        f"*Model: {results[0].model_used if results else 'unknown'} | "
        f"Total: {total_duration:.0f}s across {len(results)} agent(s)*"
    )

    return "\n".join(lines)


def format_terminal_output(results: list[RunResult]) -> str:
    lines = [
        "=" * 60,
        "E2E AGENT TEST RESULTS",
        "=" * 60,
    ]

    for result in results:
        lines.append("")
        lines.append(f"[{result.status.upper()}] {result.agent_name} ({result.area})")
        lines.append(f"  Steps: {result.steps_used}/{result.max_steps} | Time: {result.duration_seconds:.0f}s")

        if result.errors:
            for err in result.errors[:3]:
                lines.append(f"  ERROR: {err[:200]}")

        if result.raw_findings:
            lines.append("")
            lines.append(result.raw_findings)

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
