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
  - get_connected_slack_channels
index: "Slack messaging -- list channels, read messages, read threads"
rca_priority: 50
metadata:
  author: aurora
  version: "1.0"
---

# Slack Tools

## Overview
Read-only tools for searching Slack conversations. Used during postmortem generation to gather human context (deployment decisions, communication gaps, resolution steps) and during interactive chat for incident investigation.

The tool signatures and parameters are provided to you in the tool schema — this
skill only covers *when* to use each and Slack-specific behaviour.

## Choosing a tool
- **`get_connected_slack_channels`** is the routing-decision source: the channels
  Aurora is aware of, each with a description of what it's for and which
  team/service it serves. Use it to pick which channel(s) are relevant when
  posting about an incident or notifying a team — and combine it with the
  `Slack` behaviour memory (below), which holds the team/channel routing
  *preferences* the descriptions alone don't capture.
- **`list_slack_channels`** is a live, description-less listing of channels the
  bot can access — use it for discovery (scan names/topics for a service or
  "incident"/"oncall"/"alerts"), not routing.
- **`get_channel_history` / `get_thread_replies`** read messages; scope history
  to the incident time window and follow into a thread when `reply_count > 0`.

## Strategy for Incident Investigation

1. Call `list_slack_channels` — scan names/topics for the affected service or "incident"/"oncall" keywords
2. Call `get_channel_history` on the most relevant channels, scoped to the incident time window
3. Look for messages about: deployments, rollbacks, alerts firing, team handoffs, escalations
4. If a message has `reply_count > 0` and looks relevant, call `get_thread_replies` for full context

## Slack behaviour memory

Aurora's Slack behaviour (tone, when to speak, which teams/channels to notify)
lives in a single memory entry: category `context`, title `Slack`. It is seeded
on connect and is user- and agent-editable.

- **Read it** whenever you act in Slack (it is auto-injected on Slack-sourced
  sessions, but you may also `read_memory(category='context', title='Slack')`).
- **Update it (MANDATORY) as soon as a user states a standing preference or
  protocol** — don't just acknowledge it in chat, or it's lost when the session
  ends. Use `edit_memory` / `append_to_memory` on category `context`, title
  `Slack` to record things like "be quiet in #general", "post conclusions to
  #payments-oncall", "on every incident post 'down' then 'back up' here", or a
  team → channel routing rule. Save the directive BEFORE replying, then confirm
  briefly that you recorded it. This is how Aurora learns per-team Slack policy.
- Channel-scoped directives ("for this channel only") go under the "Per-channel
  notes" section keyed by channel name — never promote them to org-wide rules.

## Limitations
- Read-only messaging today (posting is handled by the notification service and
  the @mention flow, not by these tools)
- Bot must be a member of the channel to read it
- No cross-channel search — must check channels individually by name
