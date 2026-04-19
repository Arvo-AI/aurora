from __future__ import annotations

import json
import logging
import re
from typing import Optional

from utils.db.connection_pool import db_pool


def build_manual_vm_access_segment(user_id: Optional[str]) -> str:
    """Return manual VM hints with managed key paths for agent SSH."""
    if not user_id:
        return ""

    try:
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(cur, conn, user_id, log_prefix="[ContextFetchers]"):
                    return ""
                cur.execute(
                    """
                    SELECT mv.name, mv.ip_address, mv.port, mv.ssh_username, mv.ssh_jump_command, mv.ssh_key_id,
                           ut.provider, ut.token_data
                    FROM user_manual_vms mv
                    LEFT JOIN user_tokens ut ON ut.id = mv.ssh_key_id
                    WHERE mv.user_id = %s
                    ORDER BY mv.updated_at DESC
                    LIMIT 10;
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to fetch manual VMs for user {user_id}: {e}")
        return ""

    if not rows:
        return ""

    lines: list[str] = ["MANUAL VMS (managed SSH keys auto-mounted in terminal pods):"]
    for name, ip, port, ssh_username, ssh_jump_command, ssh_key_id, provider, token_data in rows:
        label = None
        if token_data:
            try:
                parsed = json.loads(token_data) if isinstance(token_data, str) else token_data
                if isinstance(parsed, dict):
                    label = parsed.get("label")
            except Exception as e:
                logging.getLogger(__name__).debug(
                    f"Failed to parse token_data for VM '{name}' (provider={provider}): {e}"
                )

        provider_str = provider or "aurora_ssh"
        vm_key = provider_str.replace("_ssh_", "_")
        key_path = f"~/.ssh/id_{vm_key}"
        user_display = ssh_username or "<set sshUsername>"
        label_str = f" ({label})" if label else ""

        # Build the actual SSH command the agent should use
        base_cmd = f"ssh -i {key_path}"
        if ssh_jump_command:
            # Extract jump host from stored command (e.g., "ssh -J user@bastion user@target")
            jump_match = re.search(r'-J\s+(\S+)', ssh_jump_command)
            if jump_match:
                base_cmd += f" -J {jump_match.group(1)}"
        lines.append(f"- {name}{label_str}: {base_cmd} {user_display}@{ip} -p {port} \"<command>\"")

    return "\n".join(lines) + "\n"


def build_knowledge_base_memory_segment(user_id: Optional[str]) -> str:
    """Build knowledge base memory segment for system prompt.

    Fetches the org's knowledge base memory content and formats it for injection
    into the system prompt. This content is always included for authenticated users.
    """
    if not user_id:
        return ""

    kb_logger = logging.getLogger(__name__)

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT org_id FROM users WHERE id = %s", (user_id,)
            )
            user_row = cursor.fetchone()
            org_id = user_row[0] if user_row else None

            if org_id:
                cursor.execute(
                    "SELECT content FROM knowledge_base_memory WHERE org_id = %s ORDER BY updated_at DESC LIMIT 1",
                    (org_id,)
                )
            else:
                cursor.execute(
                    "SELECT content FROM knowledge_base_memory WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
                    (user_id,)
                )
            row = cursor.fetchone()

        if row and row[0] and row[0].strip():
            content = row[0].strip()
            # Escape curly braces for LangChain template compatibility
            content = content.replace("{", "{{").replace("}", "}}")

            return (
                "=" * 40 + "\n"
                "USER-PROVIDED CONTEXT (Knowledge Base Memory)\n"
                "=" * 40 + "\n"
                "The user has provided the following context that should inform your analysis:\n\n"
                f"{content}\n\n"
                "Consider this context when investigating issues and making recommendations.\n"
                "=" * 40 + "\n"
            )
    except Exception as e:
        kb_logger.warning(f"[KB] Error fetching knowledge base memory for user {user_id}: {e}")

    return ""
