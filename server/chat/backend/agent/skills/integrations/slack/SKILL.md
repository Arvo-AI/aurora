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
  - post_slack_message
index: "Slack messaging -- list channels, read messages, read threads, post messages"
rca_priority: 50
metadata:
  author: aurora
  version: "1.0"
---

# Slack Tools

## Overview
Tools for working in Slack. The investigation tools (`list_slack_channels`,
`get_channel_history`, `get_thread_replies`, `get_connected_slack_channels`) are
read-only and used during postmortem generation and interactive investigation to
gather human context (deployment decisions, communication gaps, resolution
steps). In **Agent mode** you also get `post_slack_message` to post a message or
threaded reply.

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
- **`post_slack_message`** is the one write tool. Post a new message, or set
  `thread_ts` to reply UNDER an existing message. Available in Agent mode only.

## Posting like a teammate

When you post about an incident, behave like a human on-call would — don't just
dump a card into every channel:

1. Decide **who cares** using `get_connected_slack_channels` + the `Slack` memory.
   If no channel is relevant, **stay silent** (post nothing).
2. Before posting, **read the recent history** of the target channel
   (`get_channel_history`) and check whether this incident is already being
   discussed — your own earlier message, an incident.io/PagerDuty thread, or a
   human asking about it.
3. **Recurring incident** (you've seen it before — check the Incident Index in
   your prompt and your prior messages): reply IN THE THREAD of the existing
   message with a short note (e.g. "Still happening — 3rd time today, same DB
   pool exhaustion") using `thread_ts`. Do **not** start a new top-level message.
4. **New incident**: post a new, short message. Match the channel's preferred
   format from the `Slack` memory (some teams want a plain human line, others a
   short structured summary).
5. Keep it terse. One or two lines beats a wall of text.

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
- **Update it** when the team states a preference: use `edit_memory` /
  `append_to_memory` to record things like "be quiet in #general", "post
  conclusions to #payments-oncall", or a team → channel routing rule. This is
  how Aurora learns per-team Slack policy over time.
- Keep channel-specific preferences under the "Per-channel notes" section.

### Learn the service → channel map
The single highest-signal routing input is *which team channel owns which
service*. The `Slack` memory has a "Service -> channel routing map" section for
exactly this:

- **Consult it FIRST** when routing an incident: if the affected service already
  has a mapping (e.g. `payments -> #payments-oncall`), post there directly —
  don't re-scan every channel description.
- **Record it** when you route an incident to a channel because that channel owns
  the affected service: `append_to_memory` the mapping (`<service> -> #<channel>`)
  so the next incident on that service routes instantly.
- **Correct it** if a team redirects you ("this is actually the checkout team's")
  — update the mapping rather than leaving the wrong one.

This is learned behaviour: the map starts empty and gets better every incident.

## Limitations
- `post_slack_message` posts a plain mrkdwn message (or threaded reply); rich
  interactive cards are still posted by the notification service, not the agent
- Bot must be a member of the channel to read it (posting auto-joins on
  not_in_channel)
- No cross-channel search — must check channels individually by name
