from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, List, Optional, Tuple

# Prefix Cache Configuration
PREFIX_CACHE_EPHEMERAL_TTL = 300  # 5 minutes - TTL for ephemeral cache segments

# Providers that support CLI execution via cloud_exec.
# Providers not in this set (e.g. grafana) are observation-only and should
# never be passed as the provider argument to cloud_exec.
CLOUD_EXEC_PROVIDERS = frozenset({
    "gcp", "aws", "azure", "ovh", "scaleway", "tailscale",
})

from chat.backend.agent.utils.prefix_cache import PrefixCacheManager
from utils.db.connection_pool import db_pool


@dataclass
class PromptSegments:
    system_invariant: str
    provider_constraints: str
    regional_rules: str
    ephemeral_rules: str
    long_documents_note: str
    provider_context: str
    prerequisite_checks: str
    terraform_validation: str
    model_overlay: str
    failure_recovery: str
    manual_vm_access: str = ""  # Manual VM access hints with managed keys
    background_mode: str = ""  # Background chat autonomous operation instructions
    knowledge_base_memory: str = ""  # User's knowledge base memory context
    integration_index: str = ""  # Skills-based: compact index of connected integrations


def _normalize_providers(provider_preference: Optional[Any]) -> List[str]:
    if provider_preference is None:
        return []
    if isinstance(provider_preference, str):
        provider_iterable = [provider_preference]
    elif isinstance(provider_preference, list):
        provider_iterable = provider_preference
    else:
        provider_iterable = []

    normalized: List[str] = []
    for item in provider_iterable:
        if not item:
            continue
        candidate = str(item).strip().lower()
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def build_provider_constraints(provider_preference: Optional[Any]) -> Tuple[str, str, str]:
    """Return provider_text, provider_restrictions, and combined provider_constraints segment."""
    normalized = _normalize_providers(provider_preference)

    if normalized:
        if len(normalized) == 1:
            provider_text = f"the {normalized[0]} cloud"
            provider_restrictions = f"- You can ONLY access tools for the {normalized[0]} provider\n"
        else:
            provider_list = ", ".join(normalized)
            provider_text = f"multiple clouds: {provider_list}"
            provider_restrictions = f"- You can access tools for the following providers: {provider_list}\n"
    else:
        provider_text = "no specific cloud"
        provider_restrictions = "- If no provider is selected, you have limited tool access\n"

    provider_constraints = (
        f"IMPORTANT: You are currently operating on {provider_text}. "
        "All resources you create or manage MUST be for the selected provider(s). For example, if the provider is 'azure', use 'azurerm' resources. If it is 'gcp', use 'google' resources.\n\n"
        "PROVIDER RESTRICTIONS:\n"
        f"{provider_restrictions}"
        "- If no provider is selected, you have limited tool access\n"
        "- All cloud operations are restricted to the user's selected provider(s)\n"
        "- No fallbacks or cross-provider operations are allowed unless multiple providers are explicitly selected\n"
    )
    return provider_text, provider_restrictions, provider_constraints


def build_provider_context_segment(provider_preference: Optional[Any], selected_project_id: Optional[str], mode: Optional[str] = None) -> str:
    normalized = _normalize_providers(provider_preference)
    normalized_mode = (mode or "agent").strip().lower()
    
    if not normalized and not selected_project_id:
        return ""

    parts: List[str] = ["PROVIDER CONTEXT:\n"]

    if normalized:
        providers_text = ", ".join(normalized)
        parts.append(
            f"- Provider already selected: {providers_text}. Do NOT ask the user to choose a provider again; continue with these settings.\n"
        )
        # Add explicit instruction about which provider to use for cloud_exec
        # Only include providers that actually support CLI execution
        cloud_exec_providers = [p for p in normalized if p in CLOUD_EXEC_PROVIDERS]
        if len(cloud_exec_providers) == 1:
            parts.append(
                f"- IMPORTANT: Use provider='{cloud_exec_providers[0]}' for all cloud_exec calls.\n"
            )

    if selected_project_id:
        parts.append(
            f"- Active project/subscription: {selected_project_id}. Reuse this identifier in every command or Terraform manifest instead of placeholders.\n"
        )
    else:
        for provider in normalized or ["unknown"]:
            if provider == "gcp":
                parts.append(
                    "- IMPORTANT: If the user explicitly specifies a GCP project, set it as active: cloud_exec('gcp', 'config set project PROJECT_ID').\n"
                    "- Only if NO project is specified by the user, fetch the current project: cloud_exec('gcp', 'config get-value project'). Use the returned value immediately.\n"
                )
            elif provider == "aws":
                parts.append(
                    "- **MULTI-ACCOUNT AWS**: You have multiple AWS accounts connected.\n"
                    "  1. Your FIRST cloud_exec('aws', ...) call (without account_id) automatically queries ALL accounts in parallel and returns `results_by_account`.\n"
                    "  2. Review the per-account results to identify which account(s) are relevant.\n"
                    "  3. For ALL subsequent calls, pass `account_id='<ACCOUNT_ID>'` to target only the relevant account(s). Example: cloud_exec('aws', 'ec2 describe-instances', account_id='123456789012')\n"
                    "  4. NEVER keep querying all accounts after you've identified the relevant one -- it wastes time and adds noise.\n"
                    "- Fetch the AWS account ID before writing Terraform: cloud_exec('aws', \"sts get-caller-identity --query 'Account' --output text\", account_id='<ACCOUNT_ID>'). Store and reuse that output.\n"
                )
            elif provider == "azure":
                parts.append(
                    "- Fetch the Azure subscription before writing Terraform: cloud_exec('azure', \"account show --query 'id' -o tsv\"). Use the concrete subscription ID in code.\n"
                )
    # Provider-specific reference guides are now in skill files.
    # The agent loads them on-demand via load_skill().

    return "".join(parts)


def build_prerequisite_segment(provider_preference: Optional[Any], selected_project_id: Optional[str]) -> str:
    normalized = _normalize_providers(provider_preference)
    missing_project = not selected_project_id and ("gcp" in normalized or "azure" in normalized or "aws" in normalized)

    if not missing_project:
        return ""

    lines = [
        "MANDATORY CONTEXT LOOKUP:\n",
        "Before producing Terraform or CLI changes you MUST gather the live identifiers and replace any placeholders immediately.\n",
    ]
    if "gcp" in normalized:
        lines.append(
            "- Run cloud_exec('gcp', 'config get-value project') and store the exact project ID for reuse.\n"
        )
    if "aws" in normalized:
        lines.append(
            "- Run cloud_exec('aws', \"sts get-caller-identity --query 'Account' --output text\") before writing Terraform.\n"
        )
    if "azure" in normalized:
        lines.append(
            "- Run cloud_exec('azure', \"account show --query 'id' -o tsv\") so Terraform uses the real subscription.\n"
        )
    lines.append("Do not draft Terraform until these values are known.\n")
    return "".join(lines)


def _has_terraform_placeholders(terraform_code: str) -> bool:
    if not terraform_code:
        return False
    lowered = terraform_code.lower()
    placeholder_tokens = [
        "<project", "project-id", "your-project", "placeholder", "todo_", "replace", "subscription_id",
    ]
    return any(token in lowered for token in placeholder_tokens)


def build_terraform_validation_segment(state: Optional[Any]) -> str:
    if not state:
        return ""

    terraform_code = getattr(state, 'terraform_code', None)
    runtime_flag = bool(getattr(state, 'placeholder_warning', False))
    if not terraform_code and not runtime_flag:
        return ""

    needs_attention = runtime_flag or _has_terraform_placeholders(terraform_code or "")
    note_header = "TERRAFORM VALIDATION:\n"
    if needs_attention:
        details = (
            "- Terraform code still contains placeholders. Fetch the real identifiers with tool calls now and update the manifest before replying.\n"
            "- Re-run the relevant discovery commands (cloud_exec or iac_tool plan) until every identifier is concrete.\n"
        )
    else:
        details = (
            "- Double-check that every identifier (project, region, subscription, account) matches live data retrieved via tools before finalizing.\n"
        )
    return note_header + details


def build_model_overlay_segment(model: Optional[str], provider_preference: Optional[Any]) -> str:
    if not model:
        return ""
    model_lower = model.lower()
    if "gemini" not in model_lower:
        return ""

    normalized = _normalize_providers(provider_preference)
    provider_text = ", ".join(normalized) if normalized else "selected providers"
    return (
        "MODEL ADAPTATION (GEMINI):\n"
        "- Gemini often omits prerequisite tool calls. Autonomously gather missing project, subscription, or account identifiers for "
        f"{provider_text} before producing Terraform or CLI results.\n"
        "- Never leave placeholders or TODO notes; call cloud_exec or iac_tool immediately when data is unknown.\n"
    )


def build_failure_recovery_segment(state: Optional[Any]) -> str:
    if not state:
        return ""

    failure = getattr(state, 'last_tool_failure', None)
    if not failure:
        return ""

    tool_name = failure.get('tool_name') or 'a recent tool'
    command = failure.get('command')
    message = failure.get('message')

    parts = [
        "FAILURE RECOVERY:\n",
        f"- The last command from {tool_name} failed. Investigate the error and immediately apply a fix using your available tools.\n",
        "- Diagnose the failure (missing API/service, permission, invalid flag, unavailable region, etc.) and run the corrective command yourself.\n",
        "- After applying the fix, rerun the original workflow step before responding to the user.\n",
    ]

    if command:
        parts.append(f"- Command that failed: {command}\n")
    if message:
        parts.append(f"- Error summary: {message[:200]}\n")

    parts.append(
        "- For cloud API or permission errors: enable the required service (e.g., cloud_exec('gcp', 'services enable <api>'), cloud_exec('aws', 'iam attach-role-policy ...'), cloud_exec('azure', 'provider register ...')), then retry.\n"
    )
    parts.append(
        "- For Terraform plan/apply failures: run terraform init/plan/apply again via iac_tool after fixing the root cause (credentials, state, missing variables).\n"
    )
    parts.append(
        "- For CLI syntax issues: adjust flags or parameters and rerun the corrected command instead of asking the user.\n"
    )
    parts.append(
        "- For OVH failures: Use Context7 MCP with the CORRECT library based on what failed:\n"
        "  * If `iac_tool` failed → `/ovh/terraform-provider-ovh` with topic = resource type (e.g., 'ovh_cloud_project_instance')\n"
        "  * If `cloud_exec` failed → `/ovh/ovhcloud-cli` with topic = CLI command (e.g., 'cloud instance create')\n"
    )
    parts.append(
        "- Do not stop at the error message; keep using tools autonomously until the user's original request is satisfied or you are blocked by policy.\n"
    )

    return "".join(parts)


def build_system_invariant() -> str:
    """Load core system prompt from modular markdown files under skills/core/.

    Segments are loaded in a fixed order that mirrors the original monolithic
    prompt so that cached prefixes remain stable across deployments.
    """
    import os
    from chat.backend.agent.skills.loader import load_core_prompt

    core_dir = os.path.join(
        os.path.dirname(__file__), os.pardir, "skills", "core"
    )
    core_dir = os.path.normpath(core_dir)

    # Order matters for prefix-cache stability — do not reorder without
    # invalidating existing caches.
    return load_core_prompt(core_dir, segments=[
        "identity",
        "knowledge_base",
        "tool_selection",
        "ssh_access",
        "cloud_access",
        "error_handling",
        "investigation",
        "behavioral_rules",
    ])


def build_regional_rules() -> str:
    return (
        "REGION AND ZONE SELECTION - CRITICAL:\n"
        "When user specifies geographic requirements, honor them in terraform code:\n"
        "- North America (non-US): northamerica-northeast1-a or northamerica-northeast2-a (Canada)\n"
        "- Europe: europe-west1-a (Belgium) or europe-west2-a (London)\n"
        "- Asia: asia-southeast1-a (Singapore) or asia-northeast1-a (Tokyo)\n"
        "- US: Use US regions only if explicitly requested or if no geography specified\n"
        "Do not just add comments; actually use the correct zone in code.\n"
    )


def build_manual_vm_access_segment(user_id: Optional[str]) -> str:
    """Return manual VM hints with managed key paths for agent SSH."""
    if not user_id:
        return ""

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
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
    except Exception:
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
            except Exception:
                pass

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


def build_ephemeral_rules(mode: Optional[str]) -> str:
    normalized_mode = (mode or "agent").strip().lower()
    
    if normalized_mode == "ask":
        return (
            "━━━ CRITICAL: CURRENT MODE ━━━\n"
            "MODE: ASK (READ-ONLY)\n\n"
            "The user wants answers without making any infrastructure changes. "
            "Only perform READ-ONLY operations. It is acceptable to call tools that list, describe, or fetch data, "
            "but NEVER create, modify, or delete resources. Avoid iac_tool, especially the apply action, or mutating cloud_exec commands.\n\n"
            "CRITICAL PROVIDER SELECTION:\n"
            "- Use provider='gcp' for real GCP projects and GKE clusters\n"
            "- Use provider='aws' for AWS resources\n"
            "- Use provider='azure' for Azure resources\n"
            "\n"
            "IMPORTANT:\n"
            "- Before running commands, get the CURRENT project: cloud_exec('gcp', 'config get-value project')\n"
            "- Use the project returned by that command, NOT any project from conversation history.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    return (
        "━━━ CRITICAL: CURRENT MODE ━━━\n"
        "MODE: AGENT (FULL ACCESS TO CONNECTED PROVIDERS)\n\n"
        "You are operating in AGENT mode RIGHT NOW with full access to the user's connected cloud providers. "
        "You CAN and SHOULD create, modify, and delete resources on real cloud infrastructure (gcp, aws, azure).\n\n"
        "CRITICAL PROVIDER SELECTION:\n"
        "- Use provider='gcp' for real GCP projects and GKE clusters\n"
        "- Use provider='aws' for AWS resources\n"
        "- Use provider='azure' for Azure resources\n"
        "\n"
        "IMPORTANT:\n"
        "- Before running commands, get the CURRENT project: cloud_exec('gcp', 'config get-value project')\n"
        "- Use the project returned by that command, NOT any project from conversation history.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_long_documents_note(has_zip_reference: bool) -> str:
    if has_zip_reference:
        return (
            "LONG DOCUMENTS: The user referenced a ZIP/document. Use analyze_zip_file operations when asked (list/analyze/extract).\n"
        )
    return ""

def build_web_search_note() -> str: #mainly for testing
    return (
        "WEB SEARCH: Use web_search to find current solutions and best practices.\n"
        "web_search(query, provider_filter, top_k, verify) - Search for current documentation and best practices\n"
        "If you are unsure, use web_search to find the information you need.\n"
    )


def build_background_mode_segment(state: Optional[Any]) -> str:
    """Build background mode instructions for RCA or prediscovery chats."""
    if not state:
        return ""

    if not getattr(state, 'is_background', False):
        return ""

    rca_context = getattr(state, 'rca_context', None)
    if not rca_context:
        return ""

    source = rca_context.get('source', '').lower()
    providers = rca_context.get('providers', [])
    providers_lower = [p.lower() for p in providers] if providers else []
    integrations = rca_context.get('integrations', {})

    parts = [
        "=" * 40,
        "BACKGROUND RCA MODE",
        "=" * 40,
        "",
        f"Source: {'USER-REPORTED INCIDENT' if source == 'chat' else f'{source.upper()} alert'} | Providers: {', '.join(providers) if providers else 'None'}",
        "",
    ]

    # Provider CLI commands and integration guidance loaded from skill files
    parts.extend([
        "",
        f"Providers: {', '.join(providers) if providers else 'none'} — use load_skill for detailed commands.",
        "TOOLS: cloud_exec for cloud CLI | terminal_exec for shell commands",
    ])

    # Load integration-specific RCA guidance from skill files
    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        user_id = rca_context.get('user_id', '')
        registry = SkillRegistry.get_instance()
        rca_skills_content = registry.load_skills_for_rca(
            user_id=user_id,
            source=source,
            providers=providers,
            integrations=integrations,
        )
        if rca_skills_content:
            parts.extend(["", rca_skills_content])
    except Exception as e:
        import logging
        logging.warning(f"Failed to load RCA skills: {e}")

    # Integration-specific guidance (Splunk, Datadog, GitHub, Jira, etc.)
    # now loaded from skill files above via SkillRegistry.load_skills_for_rca().

    # Knowledge Base search (always available for authenticated users)
    parts.extend([
        "",
        "KNOWLEDGE BASE:",
        "Use knowledge_base_search tool to find runbooks and documentation:",
        "- knowledge_base_search(query='service name error') - Search for relevant docs",
        "- Check KB FIRST for troubleshooting procedures before cloud investigation",
    ])

    # OpenSSH fallback
    parts.extend([
        "",
        "VM ACCESS - Automatic SSH Keys Available, use OpenSSH terminal_exec to SSH into VMs:",
        "For OVH/Scaleway VMs configured via Aurora UI: Keys auto-mounted at ~/.ssh/id_<provider>_<vm_id>",
        "SSH command: terminal_exec('ssh -i ~/.ssh/id_scaleway_<VM_ID> -o StrictHostKeyChecking=no -o BatchMode=yes root@IP \"command\"')",
        "Or simpler: terminal_exec('ssh root@IP \"command\"') - keys in ~/.ssh/ tried automatically",
        "Users: GCP=admin | AWS=ec2-user/ubuntu | Azure=azureuser | OVH=debian/ubuntu/root | Scaleway=root",
    ])

    # CONTEXT UPDATE AWARENESS - CRITICAL
    parts.extend([
        "",
        "CONTEXT UPDATE AWARENESS - CRITICAL:",
        "During RCA investigations, you may receive CORRELATED INCIDENT CONTEXT UPDATEs via SystemMessage.",
        "These updates contain NEW incident data arriving mid-investigation (PagerDuty, monitoring, etc.).",
        "",
        "When you receive a context update message:",
        "1. IMMEDIATELY pivot your investigation to incorporate the new information",
        "2. STEER your next tool calls based on the update content",
        "3. Correlate new data with previous findings to identify patterns",
        "4. Adjust your investigation path - the update may reveal the root cause or new symptoms",
        "",
        "Examples:",
        "- Update shows new error in different service → investigate that service immediately",
        "- Update contains timeline data → correlate with your previous findings",
        "- Update identifies affected resources → focus investigation on those resources",
        "",
        "Context updates are HIGH PRIORITY - they represent LIVE incident evolution.",
        "=" * 40,
        "",
    ])

    # Critical requirements - MUST complete all before stopping
    if source == 'slack':
        # Slack-specific instructions (Concise, no heavy mandatory steps)
        parts.extend([
            "",
            "SLACK CONVERSATION CONTEXT:",
            "The user's message includes 'Recent conversation context' section with previous Slack thread messages.",
            "ALWAYS review this context - users reference earlier messages with 'earlier', 'that', 'it', etc.",
            "Build on the conversation - don't ignore what was already discussed in the thread.",
            "",
            "SLACK FORMATTING REQUIREMENTS:",
            "Use Slack markdown: *bold*, _italic_, `code`, ```code blocks```",
            "Structure responses: *Section Headers* + bullet points (•) or numbered lists",
            "Keep paragraphs short (2-3 sentences max)",
            "NO HTML, NO dropdowns, NO complex UI - plain text only",
            "",
            "INVESTIGATION GUIDANCE:",
            "Use tools when needed: kubectl, cloud commands, logs, metrics",
            "Check actual state with tool calls - don't assume",
            "For troubleshooting: Investigate thoroughly (resources → logs → root cause → fix)",
            "For info requests: Answer directly if you have the data",
            "Include specific evidence: exact errors, metrics, timestamps, resource names",
            "",
        ])
    elif source == 'google_chat':
        parts.extend([
            "",
            "GOOGLE CHAT CONVERSATION CONTEXT:",
            "The user's message includes 'Recent conversation context' section with previous Google Chat thread messages.",
            "ALWAYS review this context - users reference earlier messages with 'earlier', 'that', 'it', etc.",
            "Build on the conversation - don't ignore what was already discussed in the thread.",
            "",
            "GOOGLE CHAT FORMATTING REQUIREMENTS:",
            "Use Google Chat markdown: *bold*, _italic_, `code`, ```code blocks```",
            "Structure responses: *Section Headers* + bullet points (•) or numbered lists",
            "Keep paragraphs short (2-3 sentences max)",
            "NO HTML, NO dropdowns, NO complex UI - plain text only",
            "",
            "INVESTIGATION GUIDANCE:",
            "Use tools when needed: kubectl, cloud commands, logs, metrics",
            "Check actual state with tool calls - don't assume",
            "For troubleshooting: Investigate thoroughly (resources → logs → root cause → fix)",
            "For info requests: Answer directly if you have the data",
            "Include specific evidence: exact errors, metrics, timestamps, resource names",
            "",
        ])
        # Reuse provider commands, fallbacks, and SSH troubleshooting from shared functions
        parts.extend([
            "RESPONSE FORMAT:",
            "1. *Direct answer* - respond to the question immediately (1-2 sentences)",
            "2. *Evidence* - show findings from investigation or supporting data",
            "3. *Next steps* - actionable recommendations if needed (numbered list)",
            "",
            "READ-ONLY mode - investigate only, no changes unless explicitly requested.",
            "=" * 40,
        ])
    else:
        parts.extend([
            "",
            "MANDATORY INVESTIGATION STEPS - DO NOT STOP UNTIL ALL ARE DONE:",
            f"1. List resources from EVERY provider: {', '.join(providers) if providers else 'None'}",
            "2. SSH into at least one affected VM (use OpenSSH terminal_exec above)",
            "3. Check system metrics: top, free -m, df -h, dmesg | tail",
            "4. Check logs: journalctl, /var/log/, cloud logging",
            "5. Identify root cause with evidence",
            "6. Provide remediation steps",
            "",
            "YOU MUST make 15-20+ tool calls. After EACH tool call, continue investigating.",
        ])

        # Non-Anthropic models often don't produce text between tool calls unless instructed to
        model_name = (getattr(state, 'model', '') or '').lower()
        if model_name and not model_name.startswith("anthropic/"):
            parts.extend([
                "THINK OUT LOUD: Before each tool call, briefly state what you're investigating and why (1-2 sentences).",
                "After each tool result, briefly state your findings before the next tool call.",
            ])

        parts.extend([
            "NEVER stop after listing resources - that's just step 1.",
            "On failure: try 3-4 alternatives immediately.",
            "",
            "READ-ONLY mode - investigate only, no changes.",
            "=" * 40,
        ])

    return "\n".join(parts)


def build_knowledge_base_memory_segment(user_id: Optional[str]) -> str:
    """Build knowledge base memory segment for system prompt.

    Fetches the org's knowledge base memory content and formats it for injection
    into the system prompt. This content is always included for authenticated users.
    """
    if not user_id:
        return ""

    import logging
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


def build_prompt_segments(provider_preference: Optional[Any], mode: Optional[str], has_zip_reference: bool, state: Optional[Any] = None) -> PromptSegments:
    _, _, provider_constraints = build_provider_constraints(provider_preference)

    # Build system invariant
    system_invariant = build_system_invariant()
    
    provider_context = build_provider_context_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
        mode=mode,
    )

    prerequisite_checks = build_prerequisite_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
    )

    terraform_validation = build_terraform_validation_segment(state)

    model_overlay = build_model_overlay_segment(
        getattr(state, 'model', None) if state else None,
        provider_preference=provider_preference,
    )

    failure_recovery = build_failure_recovery_segment(state)
    manual_vm_access = build_manual_vm_access_segment(getattr(state, "user_id", None))

    # Build background mode segment if applicable (for RCA background chats)
    background_mode = build_background_mode_segment(state)

    # Build skills-based integration index (agent loads details via load_skill tool)
    integration_index = ""
    if state and hasattr(state, 'user_id'):
        try:
            from chat.backend.agent.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            integration_index = registry.build_index(state.user_id)
        except Exception as e:
            import logging
            logging.warning(f"Failed to build skills index: {e}")

    # Build knowledge base memory context for authenticated users
    knowledge_base_memory = ""
    if state and hasattr(state, 'user_id'):
        knowledge_base_memory = build_knowledge_base_memory_segment(state.user_id)

    return PromptSegments(
        system_invariant=system_invariant,
        provider_constraints=provider_constraints,
        regional_rules=build_regional_rules(),
        ephemeral_rules=build_ephemeral_rules(mode),
        long_documents_note=build_long_documents_note(has_zip_reference),
        provider_context=provider_context,
        prerequisite_checks=prerequisite_checks,
        terraform_validation=terraform_validation,
        model_overlay=model_overlay,
        failure_recovery=failure_recovery,
        background_mode=background_mode,
        manual_vm_access=manual_vm_access,
        knowledge_base_memory=knowledge_base_memory,
        integration_index=integration_index,
    )


def assemble_system_prompt(segments: PromptSegments) -> str: #main prompt builder
    parts: List[str] = []
    # Background mode comes first if present (important RCA context)
    if segments.background_mode:
        parts.append(segments.background_mode)
    # Knowledge base memory comes early (user-provided context for all investigations)
    if segments.knowledge_base_memory:
        parts.append(segments.knowledge_base_memory)
    if segments.ephemeral_rules:
        parts.append(segments.ephemeral_rules)
    if segments.model_overlay:
        parts.append(segments.model_overlay)
    if segments.provider_context:
        parts.append(segments.provider_context)
    if segments.manual_vm_access:
        parts.append(segments.manual_vm_access)
    # Skills-based: compact index of connected integrations
    if segments.integration_index:
        parts.append(segments.integration_index)
    if segments.prerequisite_checks:
        parts.append(segments.prerequisite_checks)
    parts.append(segments.system_invariant)
    parts.append(segments.provider_constraints)
    parts.append(segments.regional_rules)
    if segments.long_documents_note:
        parts.append(segments.long_documents_note)
    if segments.terraform_validation:
        parts.append(segments.terraform_validation)
    if segments.failure_recovery:
        parts.append(segments.failure_recovery)
    return "\n".join(parts)


def register_prompt_cache_breakpoints(
    pcm: PrefixCacheManager,
    segments: PromptSegments,
    tools: List[Any],
    provider: str,
    tenant_id: str,
) -> None:
    # Cache stable segments with regular TTL
    pcm.register_segment(
        segment_name="system_invariant",
        content=segments.system_invariant,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="provider_constraints",
        content=segments.provider_constraints,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="regional_rules",
        content=segments.regional_rules,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    if segments.provider_context:
        pcm.register_segment(
            segment_name="provider_context",
            content=segments.provider_context,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.integration_index:
        pcm.register_segment(
            segment_name="integration_index",
            content=segments.integration_index,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.prerequisite_checks:
        pcm.register_segment(
            segment_name="prerequisite_checks",
            content=segments.prerequisite_checks,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.terraform_validation:
        pcm.register_segment(
            segment_name="terraform_validation",
            content=segments.terraform_validation,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.model_overlay:
        pcm.register_segment(
            segment_name="model_overlay",
            content=segments.model_overlay,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.failure_recovery:
        pcm.register_segment(
            segment_name="failure_recovery",
            content=segments.failure_recovery,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    # Tie tool schema/version into a dedicated segment so cache invalidates when tool defs change
    pcm.register_segment(
        segment_name="tools_manifest",
        content="Tool definitions and parameter shapes",
        provider=provider,
        tenant_id=tenant_id,
        tools=tools,
        ttl_s=None,
    )
    # Ephemeral rules are not cached (or can be set to very short TTL if desired)
    if segments.ephemeral_rules:
        pcm.register_segment(
            segment_name="ephemeral_rules",
            content=segments.ephemeral_rules,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        ) 
