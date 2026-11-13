"""The four parts of a stack Aurora needs to see into.

Mirrors client/src/lib/onboarding/stack.ts; keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StackPart:
    id: str
    short: str  # "Alerts", as the setup page labels it
    noun: str  # "your alerts", used in sentences
    ask: str  # what to connect, as an instruction
    examples: str
    payoff: str  # what Aurora can do once it's connected
    providers: frozenset[str]


STACK: tuple[StackPart, ...] = (
    StackPart(
        id="alerts",
        short="Alerts",
        noun="your alerts",
        ask="connect where your alerts come from",
        examples="PagerDuty, Datadog, Grafana, Opsgenie",
        payoff="When an alert fires, Aurora starts investigating right away.",
        providers=frozenset({
            "pagerduty", "opsgenie", "incidentio", "servicenow", "bigpanda", "datadog",
            "grafana", "newrelic", "dynatrace", "sentry", "splunk", "elastic", "coroot",
            "netdata", "cloudwatch", "thousandeyes",
        }),
    ),
    StackPart(
        id="cloud",
        short="Cloud",
        noun="where your services run",
        ask="connect where your services run",
        examples="AWS, Google Cloud, Azure, Kubernetes",
        payoff="Aurora can now read the logs, metrics and resources behind an alert.",
        providers=frozenset({
            "aws", "gcp", "azure", "kubectl", "onprem", "cloudflare", "flyio", "ovh",
            "scaleway", "tailscale",
        }),
    ),
    StackPart(
        id="code",
        short="Code",
        noun="your code",
        ask="connect your code",
        examples="GitHub, GitLab, Bitbucket",
        payoff="Aurora can now tie an incident to the deploy or commit behind it.",
        providers=frozenset({"github", "gitlab", "bitbucket"}),
    ),
    StackPart(
        id="chat",
        short="Chat",
        noun="your team chat",
        ask="bring Aurora into your team chat",
        examples="Slack, Microsoft Teams, Google Chat",
        payoff="Aurora will post what it finds where your team already talks.",
        providers=frozenset({"slack", "teams", "google_chat"}),
    ),
)

PARTS = {p.id: p for p in STACK}

PROVIDER_LABELS = {
    "pagerduty": "PagerDuty", "opsgenie": "Opsgenie", "incidentio": "incident.io",
    "servicenow": "ServiceNow", "bigpanda": "BigPanda", "datadog": "Datadog",
    "grafana": "Grafana", "newrelic": "New Relic", "dynatrace": "Dynatrace",
    "sentry": "Sentry", "splunk": "Splunk", "elastic": "Elastic", "coroot": "Coroot",
    "netdata": "Netdata", "cloudwatch": "CloudWatch", "thousandeyes": "ThousandEyes",
    "aws": "AWS", "gcp": "Google Cloud", "azure": "Azure", "kubectl": "Kubernetes",
    "onprem": "your servers", "github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket",
    "cloudflare": "Cloudflare", "flyio": "Fly.io", "ovh": "OVH", "scaleway": "Scaleway",
    "tailscale": "Tailscale", "slack": "Slack", "teams": "Microsoft Teams",
    "google_chat": "Google Chat",
}

ALL_PROVIDERS = frozenset().union(*(p.providers for p in STACK))


def label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


def parts_connected(providers: set[str] | frozenset[str]) -> list[str]:
    """Stack part ids with at least one connected provider, in STACK order."""
    return [p.id for p in STACK if p.providers & providers]


def part_of(provider: str) -> str | None:
    for p in STACK:
        if provider in p.providers:
            return p.id
    return None


def next_part(done: list[str] | set[str]) -> StackPart | None:
    return next((p for p in STACK if p.id not in done), None)


def join_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"
