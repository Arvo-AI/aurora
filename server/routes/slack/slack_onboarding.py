"""First-run experience inside Slack: welcome DM, App Home, demo incident.

The website's "Add to Slack" flow gets someone from a button to a working
bot in one screen. These are what they see in Slack right after:

* A welcome DM to the installer with a demo button and links to finish setup.
* The App Home tab: the same setup checklist, with live status.
* A demo incident (button, or "@Aurora demo"): a sample alert and a root
  cause that builds up step by step, so the first useful moment happens
  before anything is connected. It is labelled as sample data throughout.
"""

from __future__ import annotations

import logging
import os
import re
import time

from celery_config import celery_app
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""

DEMO_ACTION_ID = "aurora_onboarding_demo"
# Url buttons still send a block_actions payload; this prefix acks them silently.
LINK_ACTION_PREFIX = "aurora_onboarding_link_"

# Where alerts come from, and where root causes live. Matched against the
# provider names stored with credentials.
ALERT_PROVIDERS = {
    "pagerduty", "opsgenie", "incidentio", "servicenow", "bigpanda",
    "datadog", "grafana", "newrelic", "dynatrace", "sentry", "splunk",
    "elastic", "coroot", "netdata", "cloudwatch", "thousandeyes",
}
ROOT_CAUSE_PROVIDERS = {
    "aws", "gcp", "azure", "kubectl", "onprem", "github", "gitlab",
    "bitbucket", "cloudflare", "flyio", "ovh", "scaleway",
}

_DEMO_REQUEST_RE = re.compile(
    r"^(?:please\s+)?(?:show\s+me\s+|run\s+|try\s+|start\s+|give\s+me\s+)?(?:a\s+|the\s+)?"
    r"demo(?:\s+(?:incident|rca|alert))?(?:\s+please)?[.!?\s]*$",
    re.IGNORECASE,
)

_DEMO_STEP_DELAY_S = 1.4


def _app_url(path: str) -> str:
    return f"{FRONTEND_URL}{path}"


def is_demo_request(message: str) -> bool:
    """True for "@Aurora demo", "show me a demo", "run a demo incident", ..."""
    return bool(_DEMO_REQUEST_RE.match((message or "").strip()))


def _link_button(text: str, path: str, key: str, primary: bool = False) -> dict:
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "url": _app_url(path),
        "action_id": f"{LINK_ACTION_PREFIX}{key}",
    }
    if primary:
        button["style"] = "primary"
    return button


def _demo_button(primary: bool = True) -> dict:
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": ":zap:  See a demo incident", "emoji": True},
        "action_id": DEMO_ACTION_ID,
        "value": "demo",
    }
    if primary:
        button["style"] = "primary"
    return button


# ── Welcome DM ──────────────────────────────────────────────────────


def welcome_blocks(installer_slack_user_id: str, channel_id: str | None) -> list[dict]:
    channel = f"<#{channel_id}>" if channel_id else "your incidents channel"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Hey <@{installer_slack_user_id}>, I'm Aurora :wave:\n\n"
                    f"When an alert fires, I investigate it and post the root cause in {channel}. "
                    "You can also @mention me in any channel to ask about an incident."
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Want to see what that looks like?* The demo runs on sample data "
                    "and takes about 10 seconds."
                ),
            },
        },
        {"type": "actions", "elements": [_demo_button()]},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Two steps to a root cause on your own incidents*\n"
                    "*1.*  Connect your alerts: PagerDuty, Datadog, Grafana, incident.io and more\n"
                    "*2.*  Connect where root causes live: your cloud, Kubernetes, GitHub"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                _link_button("Finish setup", "/onboarding/slack", "setup"),
            ],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "Add me to an existing incident channel with `/invite @Aurora`.",
            }],
        },
    ]


def send_welcome_dm(access_token: str, installer_slack_user_id: str, channel_id: str | None) -> None:
    """Best-effort welcome to the person who installed Aurora."""
    if not installer_slack_user_id:
        return
    from connectors.slack_connector.client import SlackClient
    try:
        SlackClient(access_token).send_message(
            channel=installer_slack_user_id,
            text="Aurora is set up. Want to see a demo incident?",
            blocks=welcome_blocks(installer_slack_user_id, channel_id),
        )
    except Exception:
        logger.warning("[SLACK-ONBOARDING] welcome DM failed", exc_info=True)


# ── App Home ────────────────────────────────────────────────────────


def setup_status(user_id: str, org_id: str) -> dict:
    """Which alert sources and root-cause sources this org has connected."""
    from routes.connector_status import active_providers
    try:
        providers = set(active_providers(user_id, org_id or ""))
    except Exception:
        logger.warning("[SLACK-ONBOARDING] provider lookup failed", exc_info=True)
        providers = set()
    return {
        "alerts": sorted(providers & ALERT_PROVIDERS),
        "root_cause": sorted(providers & ROOT_CAUSE_PROVIDERS),
    }


_PROVIDER_LABELS = {
    "pagerduty": "PagerDuty", "opsgenie": "Opsgenie", "incidentio": "incident.io",
    "servicenow": "ServiceNow", "bigpanda": "BigPanda", "datadog": "Datadog",
    "grafana": "Grafana", "newrelic": "New Relic", "dynatrace": "Dynatrace",
    "sentry": "Sentry", "splunk": "Splunk", "elastic": "Elastic", "coroot": "Coroot",
    "netdata": "Netdata", "cloudwatch": "CloudWatch", "thousandeyes": "ThousandEyes",
    "aws": "AWS", "gcp": "Google Cloud", "azure": "Azure", "kubectl": "Kubernetes",
    "onprem": "On-prem", "github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket",
    "cloudflare": "Cloudflare", "flyio": "Fly.io", "ovh": "OVH", "scaleway": "Scaleway",
}


def _labels(providers: list[str]) -> str:
    return ", ".join(_PROVIDER_LABELS.get(p, p) for p in providers)


def _checklist_row(done: bool, title: str, detail: str, button: dict | None) -> dict:
    mark = ":white_check_mark:" if done else ":white_circle:"
    block = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"{mark}  *{title}*\n{detail}"},
    }
    if button and not done:
        block["accessory"] = button
    return block


def home_view(status: dict, channel_id: str | None) -> dict:
    alerts, root_cause = status["alerts"], status["root_cause"]
    done = 1 + bool(alerts) + bool(root_cause)
    channel = f"<#{channel_id}>" if channel_id else "your incidents channel"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Aurora, your AI SRE", "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "I investigate alerts and post the root cause before anyone opens a dashboard. "
                    "@mention me in any channel to ask follow-ups."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Setup*  ·  {done} of 3 done"},
        },
        _checklist_row(True, "Slack", f"Connected. Root causes go to {channel}.", None),
        _checklist_row(
            bool(alerts), "Alerts",
            f"Connected: {_labels(alerts)}" if alerts else "PagerDuty, Datadog, Grafana, incident.io, Opsgenie…",
            _link_button("Connect", "/onboarding/slack?step=alerts", "alerts"),
        ),
        _checklist_row(
            bool(root_cause), "Cloud and code",
            f"Connected: {_labels(root_cause)}" if root_cause else "AWS, Google Cloud, Azure, Kubernetes, GitHub…",
            _link_button("Connect", "/onboarding/slack?step=root_cause", "root_cause"),
        ),
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [_demo_button(primary=done < 3), _link_button("Open Aurora", "/", "open")],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "Try `@Aurora why is checkout-api returning 500s?` in any channel I'm in.",
            }],
        },
    ]
    return {"type": "home", "blocks": blocks}


def publish_home(team_id: str, slack_user_id: str) -> None:
    """Render the App Home for whoever just opened it. Never raises."""
    from connectors.slack_connector.client import get_slack_client_for_user
    from routes.slack.slack_events_helpers import get_user_id_from_slack_team
    from utils.auth.stateless_auth import get_org_id_for_user
    from utils.secrets.secret_ref_utils import get_user_token_data

    try:
        workspace_user_id = get_user_id_from_slack_team(team_id)
        if not workspace_user_id:
            return
        client = get_slack_client_for_user(workspace_user_id)
        if not client:
            return
        org_id = get_org_id_for_user(workspace_user_id)
        token_data = get_user_token_data(workspace_user_id, "slack") or {}
        view = home_view(setup_status(workspace_user_id, org_id), token_data.get("incidents_channel_id"))
        client._make_request("POST", "views.publish", {"user_id": slack_user_id, "view": view})
    except Exception:
        logger.warning("[SLACK-ONBOARDING] App Home publish failed for team %s", sanitize(team_id), exc_info=True)


# ── Demo incident ───────────────────────────────────────────────────

_DEMO_RED = "#e01e5a"
_DEMO_GREEN = "#2eb67d"

_DEMO_STEPS = [
    "Read the PagerDuty alert and the checkout-api runbook",
    "Pulled 2,341 log lines from checkout-api (last 30 min)",
    "Compared 3 deploys from the last 2 hours",
    "Checked database connection pool metrics",
]


def _demo_alert_attachment() -> list[dict]:
    return [{
        "color": _DEMO_RED,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":red_circle:  *checkout-api error rate above 5%*\nPagerDuty  ·  triggered just now",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": "*Service*\ncheckout-api"},
                    {"type": "mrkdwn", "text": "*Severity*\nSEV-2"},
                    {"type": "mrkdwn", "text": "*Environment*\nproduction"},
                    {"type": "mrkdwn", "text": "*5xx rate*\n7.8%"},
                ],
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Demo: sample data, nothing in your stack was touched."}]},
        ],
    }]


def _demo_progress_blocks(done: int) -> list[dict]:
    lines = []
    for i, step in enumerate(_DEMO_STEPS):
        if i < done:
            lines.append(f":white_check_mark:  {step}")
        elif i == done:
            lines.append(f":hourglass_flowing_sand:  _{step}…_")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Investigating…*\n" + "\n".join(lines)}},
    ]


def _demo_rca_attachment() -> list[dict]:
    return [{
        "color": _DEMO_GREEN,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Root cause found* in 9 seconds\n\n"
                        "*Cause:* `pool_size` dropped from 50 to 10 in PR #212, deployed 1:52 PM. "
                        "At lunch traffic the pods exhaust the database pool and requests time out.\n\n"
                        "*Fix:* revert #212, or set `pool_size = 50` in `config/db.yaml`."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Evidence: 14 log lines  ·  3 metrics  ·  1 deploy diff"}],
            },
        ],
    }]


def _demo_outro_blocks() -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "That was a demo on sample data. Connect your alerts and I'll do this "
                    "on your real incidents, in your incident channel."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [_link_button("Connect your alerts", "/onboarding/slack?step=alerts", "demo_alerts", primary=True)],
        },
    ]


def _post(client, channel: str, text: str, thread_ts: str | None = None,
          blocks: list | None = None, attachments: list | None = None) -> dict:
    data: dict = {"channel": channel, "text": text}
    if thread_ts:
        data["thread_ts"] = thread_ts
    if blocks is not None:
        data["blocks"] = blocks
    if attachments is not None:
        data["attachments"] = attachments
    return client._make_request("POST", "chat.postMessage", data)


def _update(client, channel: str, ts: str, text: str,
            blocks: list | None = None, attachments: list | None = None) -> None:
    data: dict = {"channel": channel, "ts": ts, "text": text,
                  "blocks": blocks if blocks is not None else [],
                  "attachments": attachments if attachments is not None else []}
    client._make_request("POST", "chat.update", data, max_retries=1)


def run_demo_incident(team_id: str, channel: str, thread_ts: str | None = None) -> None:
    """Post the demo: alert, live investigation, root cause, next step.

    ``thread_ts`` keeps a demo asked for in a channel inside that thread, so a
    sample SEV-2 never lands as a top-level message in someone's #general.
    """
    from connectors.slack_connector.client import get_slack_client_for_user
    from routes.slack.slack_events_helpers import get_user_id_from_slack_team

    workspace_user_id = get_user_id_from_slack_team(team_id)
    client = get_slack_client_for_user(workspace_user_id) if workspace_user_id else None
    if not client:
        logger.warning("[SLACK-ONBOARDING] no Slack client for demo in team %s", sanitize(team_id))
        return

    alert = _post(client, channel, "[Demo] checkout-api error rate above 5%",
                  thread_ts=thread_ts, attachments=_demo_alert_attachment())
    # Posting to a user id opens the DM; updates need the D… channel Slack returns.
    channel = alert.get("channel") or channel
    root_ts = thread_ts or alert.get("ts")
    time.sleep(_DEMO_STEP_DELAY_S)

    progress = _post(client, channel, "Investigating…", thread_ts=root_ts, blocks=_demo_progress_blocks(0))
    progress_ts = progress.get("ts")
    for done in range(1, len(_DEMO_STEPS) + 1):
        time.sleep(_DEMO_STEP_DELAY_S)
        if done < len(_DEMO_STEPS):
            _update(client, channel, progress_ts, "Investigating…", blocks=_demo_progress_blocks(done))

    _update(client, channel, progress_ts, "Root cause found: pool_size dropped from 50 to 10 in PR #212",
            attachments=_demo_rca_attachment())
    time.sleep(_DEMO_STEP_DELAY_S / 2)
    _post(client, channel, "That was a demo on sample data.", thread_ts=root_ts, blocks=_demo_outro_blocks())


@celery_app.task(name="routes.slack.slack_onboarding.run_demo_incident_task")
def run_demo_incident_task(team_id: str, channel: str, thread_ts: str | None = None):
    """Runs the demo off the request path: Slack wants an ack within 3s."""
    try:
        run_demo_incident(team_id, channel, thread_ts)
    except Exception:
        logger.warning("[SLACK-ONBOARDING] demo incident failed in team %s", sanitize(team_id), exc_info=True)
