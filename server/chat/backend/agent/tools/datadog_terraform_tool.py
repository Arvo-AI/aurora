"""
Datadog <-> Terraform drift agent tools.

Four chat-driven tools that let the agent:
  1. List Datadog UI mutes that aren't codified in Terraform
  2. Open a PR silencing a single monitor via `datadog_downtime_schedule`
  3. Bundle every drifted silence into one PR
  4. Re-index the repo after the user merges new monitor blocks

Hard rule: these tools NEVER call Datadog write APIs. Every state change
lands as a GitHub PR that a human reviews and merges.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from chat.backend.agent.tools.github_mcp_utils import (
    build_error_response,
    build_success_response,
    call_github_mcp_sync,
    parse_file_content_response,
    parse_mcp_response,
)
from chat.backend.agent.tools.github_rca_tool import _resolve_repository
from routes.datadog.datadog_routes import (
    DatadogAPIError,
    _build_client_from_creds,
    _get_stored_datadog_credentials,
)
from services.terraform.datadog_drift_detector import compute_drift, drift_row_to_dict
from services.terraform.datadog_hcl_indexer import (
    IndexedResource,
    count_indexed_resources,
    get_repo_default_branch,
    index_repo,
    load_index,
    match_resources_to_monitor,
)

MAX_RESOURCE_NAME_LEN = 80
MAX_SLUG_LEN = 40

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------


class ListDatadogSilenceDriftArgs(BaseModel):
    """Arguments for list_datadog_silence_drift."""

    repo: Optional[str] = Field(
        default=None,
        description="Optional 'owner/repo' to scope against. Defaults to the user's connected repo (if single) or all.",
    )
    monitor_name_pattern: Optional[str] = Field(
        default=None,
        description="Optional substring filter on monitor name.",
    )
    scope: Optional[str] = Field(
        default=None,
        description="Optional exact scope filter (e.g., 'env:prod').",
    )


class SilenceDatadogMonitorViaTerraformArgs(BaseModel):
    """Arguments for silence_datadog_monitor_via_terraform."""

    monitor_name: Optional[str] = Field(
        default=None, description="Name of the Datadog monitor to silence (or pass monitor_id)."
    )
    monitor_id: Optional[int] = Field(
        default=None, description="Datadog monitor ID. Preferred over monitor_name."
    )
    scope: Optional[str] = Field(
        default=None,
        description="Scope for the silence (e.g., 'env:prod'). Defaults to '*' (all groups).",
    )
    repo: Optional[str] = Field(
        default=None,
        description="'owner/repo'. Required when the user has multiple connected repos.",
    )
    target_file: Optional[str] = Field(
        default=None,
        description="Override the target .tf file. Defaults to a sibling `downtimes.tf` next to the matched monitor.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional message override for the downtime block.",
    )


class SilenceAllDriftedMonitorsArgs(BaseModel):
    """Arguments for silence_all_drifted_monitors."""

    monitor_ids: Optional[List[int]] = Field(
        default=None,
        description="Optional subset of monitor IDs to include. Defaults to every drifted monitor.",
    )
    repo: Optional[str] = Field(
        default=None, description="'owner/repo'. Required when multiple repos are connected."
    )


class ReindexTerraformRepoArgs(BaseModel):
    """Arguments for reindex_terraform_repo."""

    repo: Optional[str] = Field(
        default=None, description="'owner/repo' to re-index. Defaults to single connected repo."
    )


# ---------------------------------------------------------------------------
# Tool 1: list drift
# ---------------------------------------------------------------------------


def list_datadog_silence_drift(
    repo: Optional[str] = None,
    monitor_name_pattern: Optional[str] = None,
    scope: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User ID is required")

    target_repo = _resolve_target_repo(user_id, repo)
    try:
        report = compute_drift(
            user_id=user_id,
            repo_full_name=target_repo,
            monitor_filter=monitor_name_pattern,
            scope_filter=scope,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[DD_TF] drift compute failed: %s", exc, exc_info=True)
        return build_error_response(f"Failed to compute drift: {exc}")

    return build_success_response(
        repo=target_repo,
        silences_total=report.silences_total,
        drift_count=len(report.drifted),
        codified_count=len(report.codified),
        drifted=[drift_row_to_dict(r) for r in report.drifted],
        codified=[drift_row_to_dict(r) for r in report.codified],
        errors=report.errors,
    )


# ---------------------------------------------------------------------------
# Tool 2: silence single monitor via PR
# ---------------------------------------------------------------------------


def silence_datadog_monitor_via_terraform(
    monitor_name: Optional[str] = None,
    monitor_id: Optional[int] = None,
    scope: Optional[str] = None,
    repo: Optional[str] = None,
    target_file: Optional[str] = None,
    message: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User ID is required")
    if not monitor_name and not monitor_id:
        return build_error_response("Provide either monitor_name or monitor_id")

    target_repo = _resolve_target_repo(user_id, repo)
    if not target_repo:
        return build_error_response(
            "Multiple repos are connected. Please pass repo='owner/repo'."
        )

    try:
        monitor = _resolve_datadog_monitor(user_id, monitor_name, monitor_id)
    except DatadogAPIError as exc:
        return build_error_response(f"Datadog lookup failed: {exc}")
    if not monitor:
        return build_error_response(
            f"Could not find Datadog monitor (name={monitor_name}, id={monitor_id})"
        )

    index = load_index(user_id, target_repo)
    if not index:
        return build_error_response(
            f"No Terraform index for {target_repo}. Run reindex_terraform_repo first."
        )

    matched, confidence = match_resources_to_monitor(
        index, monitor.get("name") or "", monitor.get("query")
    )

    if not matched and not target_file:
        return build_error_response(
            f"No matching Terraform resource for monitor '{monitor.get('name')}'. "
            "Pass target_file to write a sibling downtimes.tf in that directory.",
            match_confidence=confidence,
        )

    effective_scope = scope or "*"
    resource_name = _sanitize_resource_name(
        f"mute_{monitor.get('name') or monitor.get('id') or 'monitor'}"
    )
    block = _build_downtime_schedule_block(
        resource_name=resource_name,
        monitor_resource_address=matched.resource_address if matched else None,
        monitor_id=monitor.get("id"),
        scope=effective_scope,
        original_message=message,
        source_label="Silenced via Aurora chat" if not message else "Codified from UI mute",
    )

    write_path = target_file or _default_downtimes_path(matched)
    if not write_path:
        return build_error_response("Could not determine target file. Pass target_file.")

    owner, repo_name = target_repo.split("/", 1)
    branch_name = f"aurora/silence-{_slugify(monitor.get('name') or str(monitor.get('id')))}-{int(time.time())}"

    pr_url, err = _open_silence_pr(
        user_id=user_id,
        owner=owner,
        repo=repo_name,
        branch_name=branch_name,
        file_changes=[(write_path, block)],
        pr_title=f"Silence {monitor.get('name') or monitor.get('id')} via Terraform",
        pr_body=_single_pr_body(
            monitor_name=monitor.get("name") or str(monitor.get("id")),
            scope=effective_scope,
            codified_from_ui=message is None,
        ),
    )
    if err:
        return build_error_response(err)

    return build_success_response(
        prUrl=pr_url,
        repo=target_repo,
        branch=branch_name,
        filePath=write_path,
        monitorName=monitor.get("name"),
        monitorId=monitor.get("id"),
        scope=effective_scope,
        tfMatchConfidence=confidence,
    )


# ---------------------------------------------------------------------------
# Tool 3: bulk silence via one PR
# ---------------------------------------------------------------------------


def silence_all_drifted_monitors(
    monitor_ids: Optional[List[int]] = None,
    repo: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User ID is required")

    target_repo = _resolve_target_repo(user_id, repo)
    if not target_repo:
        return build_error_response(
            "Multiple repos are connected. Please pass repo='owner/repo'."
        )

    try:
        report = compute_drift(user_id=user_id, repo_full_name=target_repo)
    except Exception as exc:  # noqa: BLE001
        return build_error_response(f"Failed to compute drift: {exc}")

    drifted = report.drifted
    if monitor_ids:
        wanted = {int(m) for m in monitor_ids}
        drifted = [r for r in drifted if r.silence.monitor_id in wanted]

    if not drifted:
        return build_error_response("No drifted silences to codify.")

    index = report.indexed_resources

    grouped: Dict[str, List[str]] = {}
    summary_rows: List[Dict[str, Any]] = []
    for drift_row in drifted:
        monitor_name = drift_row.silence.monitor_name or str(drift_row.silence.monitor_id)
        matched = drift_row.matched_tf_resource
        if matched is None:
            matched, _ = match_resources_to_monitor(
                index, monitor_name, drift_row.silence.monitor_query
            )
        write_path = _default_downtimes_path(matched)
        if not write_path:
            summary_rows.append(
                {"monitor": monitor_name, "status": "skipped", "reason": "no TF match"}
            )
            continue
        resource_name = _sanitize_resource_name(f"mute_{monitor_name}")
        block = _build_downtime_schedule_block(
            resource_name=resource_name,
            monitor_resource_address=matched.resource_address if matched else None,
            monitor_id=drift_row.silence.monitor_id,
            scope=drift_row.silence.scope or "*",
            original_message=drift_row.silence.message,
            source_label="Codified from UI mute",
        )
        grouped.setdefault(write_path, []).append(block)
        summary_rows.append(
            {
                "monitor": monitor_name,
                "file": write_path,
                "scope": drift_row.silence.scope or "*",
                "status": "queued",
            }
        )

    if not grouped:
        return build_error_response(
            "No silences could be codified (no matching Terraform files).",
            summary=summary_rows,
        )

    owner, repo_name = target_repo.split("/", 1)
    branch_name = f"aurora/silence-drift-{int(time.time())}"
    file_changes = [
        (path, "\n\n".join(blocks)) for path, blocks in grouped.items()
    ]

    pr_url, err = _open_silence_pr(
        user_id=user_id,
        owner=owner,
        repo=repo_name,
        branch_name=branch_name,
        file_changes=file_changes,
        pr_title=f"Silence {len(drifted)} Datadog monitor(s) via Terraform",
        pr_body=_bulk_pr_body(summary_rows),
    )
    if err:
        return build_error_response(err)

    return build_success_response(
        prUrl=pr_url,
        repo=target_repo,
        branch=branch_name,
        filesTouched=list(grouped.keys()),
        resourcesAdded=sum(len(v) for v in grouped.values()),
        summary=summary_rows,
    )


# ---------------------------------------------------------------------------
# Tool 4: reindex repo
# ---------------------------------------------------------------------------


def reindex_terraform_repo(
    repo: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User ID is required")
    target_repo = _resolve_target_repo(user_id, repo)
    if not target_repo:
        return build_error_response(
            "Multiple repos are connected. Please pass repo='owner/repo'."
        )

    previous_count = count_indexed_resources(user_id, target_repo)

    try:
        summary = index_repo(user_id, target_repo)
    except Exception as exc:  # noqa: BLE001
        logger.error("[DD_TF] reindex failed: %s", exc, exc_info=True)
        return build_error_response(f"Re-index failed: {exc}")

    delta = summary.resources_indexed - previous_count
    return build_success_response(
        repo=target_repo,
        commitSha=summary.commit_sha,
        filesScanned=summary.files_scanned,
        filesSkipped=summary.files_skipped,
        resourcesIndexed=summary.resources_indexed,
        previousResources=previous_count,
        delta=delta,
        errors=summary.errors,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_target_repo(user_id: str, explicit_repo: Optional[str]) -> Optional[str]:
    """Return 'owner/repo' for the chosen repo, or None if ambiguous/missing."""
    owner, repo_name, _ = _resolve_repository(user_id, explicit_repo)
    if owner and repo_name:
        return f"{owner}/{repo_name}"
    return None


def _resolve_datadog_monitor(
    user_id: str, monitor_name: Optional[str], monitor_id: Optional[int]
) -> Optional[Dict[str, Any]]:
    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        raise DatadogAPIError("Datadog not connected")
    client = _build_client_from_creds(creds)
    if not client:
        raise DatadogAPIError("Failed to build Datadog client")

    if monitor_id is not None:
        try:
            resp = client.get_monitor(int(monitor_id))
            if isinstance(resp, dict) and resp.get("id"):
                return resp
        except DatadogAPIError as exc:
            logger.warning("[DD_TF] monitor lookup by id failed: %s", exc)

    if monitor_name:
        payload = client.list_monitors({"name": monitor_name, "page_size": 50})
        if isinstance(payload, list):
            target = monitor_name.strip().lower()
            for m in payload:
                if isinstance(m, dict) and (m.get("name") or "").strip().lower() == target:
                    return m
            if payload:
                return payload[0] if isinstance(payload[0], dict) else None
    return None


def _default_downtimes_path(matched: Optional[IndexedResource]) -> Optional[str]:
    if not matched or not matched.file_path:
        return None
    directory = matched.file_path.rsplit("/", 1)[0] if "/" in matched.file_path else ""
    return f"{directory}/downtimes.tf" if directory else "downtimes.tf"


def _sanitize_resource_name(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = f"mute_{int(time.time())}"
    if not cleaned[0].isalpha() and cleaned[0] != "_":
        cleaned = f"mute_{cleaned}"
    return cleaned.lower()[:MAX_RESOURCE_NAME_LEN]


def _slugify(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return slug[:MAX_SLUG_LEN] or "monitor"


def _build_downtime_schedule_block(
    resource_name: str,
    monitor_resource_address: Optional[str],
    monitor_id: Optional[int],
    scope: str,
    original_message: Optional[str],
    source_label: str,
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    start = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if original_message:
        message = f"{original_message} — codified from UI mute on {today}"
    else:
        message = f"{source_label} on {today}. Silenced until this block is removed."

    if monitor_resource_address:
        monitor_ref = f"    monitor_id = {monitor_resource_address}.id"
    elif monitor_id is not None:
        monitor_ref = f"    monitor_id = {int(monitor_id)}"
    else:
        monitor_ref = "    # monitor_id = <REPLACE_ME>"

    escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')

    return (
        f'resource "datadog_downtime_schedule" "{resource_name}" {{\n'
        f"  monitor_identifier {{\n"
        f"{monitor_ref}\n"
        f"  }}\n"
        f'  scope = "{scope}"\n'
        f"  schedule {{\n"
        f"    one_time_schedule {{\n"
        f'      start = "{start}"\n'
        f"      # no end → indefinite; remove this resource to lift the silence\n"
        f"    }}\n"
        f"  }}\n"
        f'  message          = "{escaped_message}"\n'
        f'  display_timezone = "UTC"\n'
        f"}}\n"
    )


def _single_pr_body(monitor_name: str, scope: str, codified_from_ui: bool) -> str:
    source_line = (
        "codified from UI mute" if codified_from_ui else "silenced via Aurora chat"
    )
    return (
        "## Why\n"
        "Aurora codifying a Datadog silence so it survives `terraform apply`.\n\n"
        "## What this PR does\n"
        f"- Adds a `datadog_downtime_schedule` resource mirroring the current silence ({source_line})\n"
        f"- Monitor: `{monitor_name}`\n"
        f"- Scope: `{scope}`\n\n"
        "## How to revert\n"
        "Delete the block and run `terraform apply`.\n"
    )


def _bulk_pr_body(summary_rows: List[Dict[str, Any]]) -> str:
    bullets = []
    for row in summary_rows:
        if row.get("status") != "queued":
            continue
        bullets.append(
            f"- `{row['monitor']}` (scope `{row.get('scope', '*')}`) → `{row['file']}`"
        )
    skipped = [r for r in summary_rows if r.get("status") == "skipped"]

    body = (
        "## Why\n"
        "Aurora codifying Datadog UI silences so they survive `terraform apply`.\n\n"
        "## What this PR does\n"
        "- Adds `datadog_downtime_schedule` resources mirroring current silences:\n"
        + ("\n".join(bullets) if bullets else "  (none)")
    )
    if skipped:
        body += "\n\n## Skipped\n" + "\n".join(
            f"- `{r['monitor']}` — {r.get('reason', 'no match')}" for r in skipped
        )
    body += "\n\n## How to revert\nDelete the block(s) and run `terraform apply`.\n"
    return body


def _open_silence_pr(
    user_id: str,
    owner: str,
    repo: str,
    branch_name: str,
    file_changes: List[Tuple[str, str]],
    pr_title: str,
    pr_body: str,
) -> Tuple[Optional[str], Optional[str]]:
    base_branch = get_repo_default_branch(user_id, f"{owner}/{repo}") or "main"

    branch_err = _create_branch(user_id, owner, repo, branch_name, base_branch)
    if branch_err:
        return None, f"Failed to create branch: {branch_err}"

    combined_files: List[Dict[str, str]] = []
    for path, new_content in file_changes:
        existing = _get_existing_file(user_id, owner, repo, path, base_branch)
        if existing:
            if not existing.endswith("\n"):
                existing += "\n"
            content = existing + "\n" + new_content
        else:
            content = new_content
        combined_files.append({"path": path, "content": content})

    push_err = _push_files(
        user_id=user_id,
        owner=owner,
        repo=repo,
        branch=branch_name,
        files=combined_files,
        message=pr_title,
    )
    if push_err:
        return None, f"Failed to push files: {push_err}"

    pr_url, pr_err = _create_pull_request(
        user_id=user_id,
        owner=owner,
        repo=repo,
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base=base_branch,
    )
    if pr_err:
        return None, f"Failed to open PR: {pr_err}"
    return pr_url, None


def _create_branch(user_id: str, owner: str, repo: str, branch: str, base: str) -> Optional[str]:
    result = call_github_mcp_sync(
        "create_branch",
        {"owner": owner, "repo": repo, "branch": branch, "from_branch": base},
        user_id,
    )
    parsed = parse_mcp_response(result)
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed["error"]
    return None


def _get_existing_file(
    user_id: str, owner: str, repo: str, path: str, branch: str
) -> Optional[str]:
    args = {"owner": owner, "repo": repo, "path": path, "ref": f"refs/heads/{branch}"}
    result = call_github_mcp_sync("get_file_contents", args, user_id)
    return parse_file_content_response(result)


def _push_files(
    user_id: str,
    owner: str,
    repo: str,
    branch: str,
    files: List[Dict[str, str]],
    message: str,
) -> Optional[str]:
    result = call_github_mcp_sync(
        "push_files",
        {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "files": files,
            "message": message,
        },
        user_id,
    )
    parsed = parse_mcp_response(result)
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed["error"]
    return None


def _create_pull_request(
    user_id: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> Tuple[Optional[str], Optional[str]]:
    result = call_github_mcp_sync(
        "create_pull_request",
        {"owner": owner, "repo": repo, "title": title, "body": body, "head": head, "base": base},
        user_id,
    )
    parsed = parse_mcp_response(result)
    if isinstance(parsed, dict) and "error" in parsed:
        return None, parsed["error"]
    pr_url = (
        parsed.get("html_url")
        or parsed.get("url")
        or parsed.get("pullRequestUrl")
        if isinstance(parsed, dict)
        else None
    )
    pr_number = parsed.get("number") if isinstance(parsed, dict) else None
    if not pr_url and pr_number:
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    if not pr_url:
        return None, "PR created but no URL returned"
    return pr_url, None
