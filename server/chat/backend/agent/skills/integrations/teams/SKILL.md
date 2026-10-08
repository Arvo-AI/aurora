---
name: teams
id: teams
description: "Microsoft Teams integration for teammate messaging during incidents and @mention replies"
category: communication
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.teams_tool
  function: is_teams_connected
tools:
  - get_connected_teams_channels
  - get_teams_channel_history
  - post_teams_message
rca_priority: 45
---

# Microsoft Teams

Aurora acts as a teammate in connected Microsoft Teams channels. Use Teams tools to list
channels, read history, and post conclusions when the org's **Microsoft Teams** memory
(`context/Microsoft Teams`) says to speak.

## Tools

- `get_connected_teams_channels` — channels Aurora may post to (with descriptions)
- `get_teams_channel_history` — recent messages in a channel (requires `team_id`, `channel_id`)
- `post_teams_message` — post or thread a reply (`reply_to_id` for follow-ups)

## RCA workflow (read-only)

Search Teams channel history for human discussion around the incident window. Do not post
during RCA unless explicitly in Agent mode and the user asks.
