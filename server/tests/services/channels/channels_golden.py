"""Golden strings captured from the pre-refactor Slack modules (main @ 539ffdbf):
``slack_tool.get_connected_slack_channels`` output for a fixed fake row set and
``slack_channel_metadata.METADATA_PROMPT`` rendered for a fixed context. A change
here is a deliberate behaviour change, never a refactor side effect."""

CONNECTED_ROWS = [
    ("C1", "payments-oncall", "team", None, "Payments on-call channel.", True),
    ("C2", "inc-2024-01", "incident", "incident.io", None, True),
]

CONNECTED_TWO_ROWS_JSON = '{"channels": [{"channel_id": "C1", "channel_name": "payments-oncall", "channel_type": "team", "detected_platform": null, "description": "Payments on-call channel.", "is_member": true}, {"channel_id": "C2", "channel_name": "inc-2024-01", "channel_type": "incident", "detected_platform": "incident.io", "description": "(no description)", "is_member": true}]}'

CONNECTED_NO_ROWS_JSON = '{"channels": [], "message": "No active Slack channels yet. Ask the user to activate channels on the Slack manage page (or invite Aurora to them in Slack and refresh), or use list_slack_channels for a live listing."}'

CONNECTED_NO_USER_JSON = '{"error": "No user context available."}'

# Whitespace-normalised SQL the pre-refactor tool ran (provider was a literal).
CONNECTED_SQL_FLAT = "SELECT DISTINCT ON (channel_id) channel_id, channel_name, channel_type, detected_platform, metadata_summary, is_member FROM slack_channels WHERE provider = 'slack' AND is_member AND metadata_status = 'ready' AND org_id = %s ORDER BY channel_id, updated_at DESC"

METADATA_TEMPLATE = "Write a 2-3 sentence description of this Slack channel for an AI SRE teammate. State what the channel is used for, which team or service it serves, and whether it's an incident/alerting channel (and from which platform if apparent, e.g. incident.io/PagerDuty/Opsgenie). Infer from the name, topic, purpose, and recent messages. Output ONLY the description — no notes, caveats, or markdown headers.\n\n{context}"

METADATA_CONTEXT = 'Channel name: #payments-oncall\nTopic: Payments alerts\nPurpose: On-call for payments\nRecent messages:\n- deploy done\n- latency spike'

METADATA_PROMPT_RENDERED = "Write a 2-3 sentence description of this Slack channel for an AI SRE teammate. State what the channel is used for, which team or service it serves, and whether it's an incident/alerting channel (and from which platform if apparent, e.g. incident.io/PagerDuty/Opsgenie). Infer from the name, topic, purpose, and recent messages. Output ONLY the description — no notes, caveats, or markdown headers.\n\nChannel name: #payments-oncall\nTopic: Payments alerts\nPurpose: On-call for payments\nRecent messages:\n- deploy done\n- latency spike"
