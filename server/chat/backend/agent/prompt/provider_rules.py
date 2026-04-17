from __future__ import annotations

from typing import Any, List, Optional, Tuple


# Providers that support CLI execution via cloud_exec.
# Providers not in this set (e.g. grafana) are observation-only and should
# never be passed as the provider argument to cloud_exec.
CLOUD_EXEC_PROVIDERS = frozenset({
    "gcp", "aws", "azure", "ovh", "scaleway", "tailscale",
})


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


def build_provider_context_segment(
    provider_preference: Optional[Any],
    selected_project_id: Optional[str],
    mode: Optional[str] = None,
) -> str:
    normalized = _normalize_providers(provider_preference)

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


def build_web_search_note() -> str:  # mainly for testing
    return (
        "WEB SEARCH: Use web_search to find current solutions and best practices.\n"
        "web_search(query, provider_filter, top_k, verify) - Search for current documentation and best practices\n"
        "If you are unsure, use web_search to find the information you need.\n"
    )
