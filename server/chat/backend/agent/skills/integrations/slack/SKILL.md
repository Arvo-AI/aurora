---
name: slack
id: slack
description: "Slack integration tools for reading channel messages and thread replies"
category: communication
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.slack_tool
  function: is_slack_connected
tools:
  - list_slack_channels
  - get_channel_history
  - get_thread_replies
index: "Slack messaging -- list channels, read messages, read threads"
rca_priority: 50
metadata:
  author: aurora
  version: "1.0"
---

# Slack Tools

## Overview
Read-only tools for searching Slack conversations. Used during postmortem generation to gather human context (deployment decisions, communication gaps, resolution steps) and during interactive chat for incident investigation.

## Tools

### `list_slack_channels()`
Returns all channels the bot can access: id, name, topic, purpose, member count. Use channel names and topics to identify relevant channels for the incident (look for service names, "incident", "oncall", "alerts").

### `get_channel_history(channel_id, oldest?, latest?, limit?)`
Fetch messages from a channel. Scope with `oldest`/`latest` (ISO 8601) to the incident time window. Returns message text, timestamps, user IDs, and thread metadata (reply_count, thread_ts).

### `get_thread_replies(channel_id, thread_ts, limit?)`
Fetch replies in a thread. Use when a message has `reply_count > 0` and looks relevant.

## Strategy for Incident Investigation

1. Call `list_slack_channels` — scan names/topics for the affected service or "incident"/"oncall" keywords
2. Call `get_channel_history` on the most relevant channels, scoped to the incident time window
3. Look for messages about: deployments, rollbacks, alerts firing, team handoffs, escalations
4. If a message has `reply_count > 0` and looks relevant, call `get_thread_replies` for full context

## Limitations
- Read-only — cannot post messages
- Bot must be a member of the channel to read it
- No cross-channel search — must check channels individually by name
