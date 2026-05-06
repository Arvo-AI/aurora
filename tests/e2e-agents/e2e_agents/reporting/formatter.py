from e2e_agents.runner.models import RunResult


def _status_icon(status: str) -> str:
    return {
        "completed": "✅",
        "timed_out": "⏱️",
        "errored": "❌",
        "crashed": "💥",
    }.get(status, "❓")


def _severity_icon(severity: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "⚪",
    }.get(severity, "⚪")


def _truncate(text: str, max_len: int = 3000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n... (truncated)"


def format_pr_comment(results: list[RunResult], labels: list[str]) -> str:
    total_duration = sum(r.duration_seconds for r in results)
    total_steps = sum(r.steps_used for r in results)
    all_issues = [i for r in results for i in r.issues]

    lines = [
        "## 🔍 E2E Agent Test Results",
        "",
        f"**Labels**: {', '.join(f'`{l}`' for l in labels)}",
        f"**Duration**: {total_duration:.0f}s | **Steps**: {total_steps} | **Issues**: {len(all_issues)}",
        "",
    ]

    # Issue summary table (if any structured issues were extracted)
    if all_issues:
        lines.append("### Issues Found")
        lines.append("")
        lines.append("| Severity | Issue | Page |")
        lines.append("|----------|-------|------|")
        for issue in sorted(all_issues, key=lambda i: ["critical", "high", "medium", "low"].index(i.severity)):
            icon = _severity_icon(issue.severity)
            desc = issue.description[:100]
            url = issue.page_url.replace("http://localhost:3000", "")
            lines.append(f"| {icon} {issue.severity} | {desc} | `{url}` |")
        lines.append("")

    # Per-agent details
    for result in results:
        icon = _status_icon(result.status)
        retry_marker = " (retried)" if result.retried else ""
        lines.append(f"### {icon} {result.agent_name} (`{result.area}`){retry_marker}")
        lines.append(
            f"**Status**: {result.status} "
            f"({result.steps_used}/{result.max_steps} steps) | "
            f"⏱️ {result.duration_seconds:.0f}s"
        )
        lines.append("")

        if result.raw_findings:
            lines.append("<details><summary>Full agent output</summary>")
            lines.append("")
            lines.append(_truncate(result.raw_findings))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        elif result.status in ("errored", "crashed"):
            lines.append(f"**Errors**: {'; '.join(result.errors[:3])}")
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
        retry = " [RETRIED]" if result.retried else ""
        lines.append(f"[{result.status.upper()}] {result.agent_name} ({result.area}){retry}")
        lines.append(f"  Steps: {result.steps_used}/{result.max_steps} | Time: {result.duration_seconds:.0f}s")

        if result.issues:
            lines.append(f"  Issues found: {len(result.issues)}")
            for issue in result.issues:
                lines.append(f"    [{issue.severity.upper()}] {issue.description[:120]}")
                lines.append(f"             → {issue.page_url}")

        if result.errors:
            for err in result.errors[:3]:
                lines.append(f"  ERROR: {err[:200]}")

        if result.raw_findings and not result.issues:
            lines.append("")
            lines.append(result.raw_findings)

    lines.append("")
    lines.append("=" * 60)

    # Summary
    total_issues = sum(len(r.issues) for r in results)
    if total_issues:
        lines.append(f"TOTAL: {total_issues} issue(s) across {len(results)} agent(s)")

    return "\n".join(lines)
