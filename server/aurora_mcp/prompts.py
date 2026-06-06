"""MCP prompts — guided workflows clients can pick from a menu."""

from __future__ import annotations


def register_prompts(mcp) -> None:

    @mcp.prompt()
    def investigate_incident(incident_id: str) -> str:
        """Structured prompt for investigating an Aurora incident."""
        return (
            f"Investigate Aurora incident #{incident_id}. Steps:\n"
            "1. get_incident to retrieve full details (summary, suggestions, citations)\n"
            "2. incident_findings to see what each RCA sub-agent investigated, then "
            "incident_finding_detail for the tools/steps a specific agent ran\n"
            "3. incident_list_alerts to review the correlated alerts\n"
            "4. Search runbooks via search_runbooks\n"
            "5. Summarize root cause, impact, recommended actions"
        )

    @mcp.prompt()
    def blast_radius_analysis(service_name: str) -> str:
        """Analyze the blast radius of a failing service."""
        return (
            f"Analyze the blast radius for service '{service_name}'.\n"
            f"1. service_impact(name='{service_name}') to get downstream dependents\n"
            f"2. list_incidents to check for active incidents on the affected services\n"
            f"3. Summarize: which services are at risk, estimated user impact,"
            f" mitigation steps"
        )

    @mcp.prompt()
    def triage_alert(alert_id: str) -> str:
        """Structured triage workflow tying alerts → logs → metrics → recent deploys."""
        return (
            f"Triage alert {alert_id}.\n"
            "1. query_alerts to look up the alert details and affected service\n"
            "2. query_logs for the affected service over the last 60 minutes\n"
            "3. query_metrics for error rate and latency on the same service\n"
            "4. Check recent deploys via call_tool('github_list_branches', ...) "
            "or query_bitbucket\n"
            "5. Decide: real incident or known noise? Recommend next step "
            "(page on-call, snooze, or trigger_rca(issue_description=…))"
        )

    @mcp.prompt()
    def summarize_incident(incident_id: str) -> str:
        """Produce a postmortem-shaped summary using existing RCA citations."""
        return (
            f"Produce a postmortem-shaped summary for incident #{incident_id}.\n"
            "1. get_incident to pull the full RCA + citations\n"
            "2. Structure: TL;DR, Impact, Timeline, Root Cause, Contributing Factors,"
            " What Went Well, Action Items\n"
            "3. Quote citations verbatim where they support claims\n"
            "4. Recommend follow-up runbook updates the team should adopt"
        )
