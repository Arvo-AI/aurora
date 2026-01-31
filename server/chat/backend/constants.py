"""Shared constants used across the chat backend."""

# Tool output truncation limit for Celery message size constraints
# Prevents large tool outputs from exceeding Redis/Celery message limits
MAX_TOOL_OUTPUT_CHARS = 5000

# Infrastructure tools that trigger visualization updates
INFRASTRUCTURE_TOOLS = frozenset(['on_prem_kubectl', 'cloud_exec', 'terminal_exec', 'tailscale_ssh'])
