"""Dispatch and complete incident-guided browser tests for exact PR heads."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from typing import Any

import requests

from utils.auth.github_app_token import get_installation_token
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)
_API = "https://api.github.com"
_HEAD_RE = __import__("re").compile(r"^[0-9a-f]{40}$")
_ACTIONS = {"opened", "reopened", "ready_for_review", "synchronize"}


def _headers(installation_id: int) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_installation_token(installation_id)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(method: str, url: str, *, installation_id: int, **kwargs):
    response = requests.request(
        method, url, headers=_headers(installation_id), timeout=30, **kwargs
    )
    if response.status_code >= 400:
        logger.error(
            "managed_browser github_request_failed method=%s status=%s path=%s",
            method,
            response.status_code,
            requests.utils.urlparse(url).path,
        )
        response.raise_for_status()
    return response


def _config(repo: str) -> dict[str, Any] | None:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, installation_id, install_command, start_command,
                      preview_port, readiness_path, readiness_timeout_seconds,
                      scenario_path, scenario_hash, model, base_branch,
                      head_prefix, runner_repo, runner_workflow, runner_ref,
                      aurora_repo, aurora_ref
                 FROM browser_test_configs
                WHERE repo_full_name = %s AND enabled = TRUE""",
            (repo,),
        )
        row = cur.fetchone()
    if not row:
        return None
    names = (
        "id", "installation_id", "install_command", "start_command",
        "preview_port", "readiness_path", "readiness_timeout_seconds",
        "scenario_path", "scenario_hash", "model", "base_branch",
        "head_prefix", "runner_repo", "runner_workflow", "runner_ref",
        "aurora_repo", "aurora_ref",
    )
    return dict(zip(names, row))


def _callback_url() -> str:
    webhook = (os.getenv("GITHUB_APP_WEBHOOK_URL") or "").strip()
    suffix = "/github/webhook"
    if not webhook.endswith(suffix):
        raise RuntimeError("GITHUB_APP_WEBHOOK_URL cannot derive browser callback URL")
    return webhook[: -len(suffix)] + "/github/browser-test/callback"


def _update_check(
    installation_id: int, repo: str, check_id: int, payload: dict[str, Any]
) -> None:
    _request(
        "PATCH",
        f"{_API}/repos/{repo}/check-runs/{check_id}",
        installation_id=installation_id,
        json=payload,
    )


def maybe_dispatch(payload: dict[str, Any], action: str | None) -> None:
    """Create a check and dispatch the central runner when a saved config matches."""
    if action not in _ACTIONS or (payload.get("pull_request") or {}).get("draft"):
        return
    repo = ((payload.get("repository") or {}).get("full_name") or "").strip()
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_sha = head.get("sha")
    pr_number = pr.get("number")
    cfg = _config(repo)
    if not cfg or not isinstance(pr_number, int) or not isinstance(head_sha, str):
        return
    if not _HEAD_RE.fullmatch(head_sha) or base.get("ref") != cfg["base_branch"]:
        return
    if (head.get("repo") or {}).get("full_name") != repo:
        return
    head_ref = str(head.get("ref") or "")
    if cfg["head_prefix"] and not head_ref.startswith(cfg["head_prefix"]):
        return

    run_id = uuid.uuid4()
    callback_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(callback_token.encode()).hexdigest()
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO browser_test_runs
                   (id, config_id, repo_full_name, installation_id, pr_number,
                    head_sha, scenario_hash, callback_token_hash)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (repo_full_name, pr_number, head_sha, scenario_hash)
                 DO NOTHING RETURNING id""",
            (str(run_id), cfg["id"], repo, cfg["installation_id"], pr_number,
             head_sha, cfg["scenario_hash"], token_hash),
        )
        inserted = cur.fetchone()
        conn.commit()
    if not inserted:
        logger.info("managed_browser skipped duplicate repo=%s pr=%s sha=%s", repo, pr_number, head_sha)
        return

    check = _request(
        "POST",
        f"{_API}/repos/{repo}/check-runs",
        installation_id=cfg["installation_id"],
        json={
            "name": "Aurora / Incident browser test",
            "head_sha": head_sha,
            "status": "queued",
            "external_id": str(run_id),
            "output": {
                "title": "Waiting for Aurora's fresh browser-test VM",
                "summary": f"Frozen scenario: `{cfg['scenario_hash']}`",
            },
        },
    ).json()
    check_id = int(check["id"])
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE browser_test_runs SET check_run_id=%s WHERE id=%s", (check_id, str(run_id)))
        conn.commit()

    inputs = {
        "run_id": str(run_id), "callback_token": callback_token,
        "callback_url": _callback_url(), "source_repo": repo,
        "pr_number": str(pr_number), "head_sha": head_sha,
        "install_command": cfg["install_command"], "start_command": cfg["start_command"],
        "preview_port": str(cfg["preview_port"]), "readiness_path": cfg["readiness_path"],
        "readiness_timeout": str(cfg["readiness_timeout_seconds"]),
        "scenario_path": cfg["scenario_path"], "scenario_hash": cfg["scenario_hash"],
        "model": cfg["model"], "aurora_repo": cfg["aurora_repo"],
        "aurora_ref": cfg["aurora_ref"],
    }
    try:
        _request(
            "POST",
            f"{_API}/repos/{cfg['runner_repo']}/actions/workflows/{cfg['runner_workflow']}/dispatches",
            installation_id=cfg["installation_id"],
            json={"ref": cfg["runner_ref"], "inputs": inputs},
        )
        _update_check(cfg["installation_id"], repo, check_id, {"status": "in_progress"})
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE browser_test_runs SET status='running' WHERE id=%s", (str(run_id),))
            conn.commit()
    except Exception as exc:
        _update_check(cfg["installation_id"], repo, check_id, {
            "status": "completed", "conclusion": "failure",
            "output": {"title": "Aurora could not start the browser-test VM", "summary": type(exc).__name__},
        })
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE browser_test_runs SET status='failed', error=%s, completed_at=NOW() WHERE id=%s", (type(exc).__name__, str(run_id)))
            conn.commit()
        raise


def complete(run_id: str, callback_token: str, result: dict[str, Any], workflow_run_id: int, workflow_url: str) -> str:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.repo_full_name,r.installation_id,r.pr_number,r.head_sha,
                      r.scenario_hash,r.callback_token_hash,r.check_run_id,r.status
                 FROM browser_test_runs r WHERE r.id=%s""", (run_id,)
        )
        row = cur.fetchone()
    if not row:
        raise LookupError("unknown run")
    repo, installation_id, pr_number, head_sha, scenario_hash, expected_hash, check_id, status = row
    supplied_hash = hashlib.sha256(callback_token.encode()).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise PermissionError("invalid callback token")
    if status in {"completed", "superseded"}:
        return status
    if result.get("testedCommit") != head_sha or result.get("scenarioHash") != scenario_hash:
        verdict, reason = "inconclusive", "Runner result did not match the frozen commit and scenario."
    else:
        verdict = result.get("verdict") if result.get("verdict") in {"passed", "failed", "inconclusive"} else "inconclusive"
        reason = str(result.get("reason") or "No result reason supplied.")
    pr_response = _request("GET", f"{_API}/repos/{repo}/pulls/{pr_number}", installation_id=installation_id)
    current_sha = ((pr_response.json().get("head") or {}).get("sha"))
    if current_sha != head_sha:
        conclusion, final_status, title = "cancelled", "superseded", "Superseded by a newer PR commit"
    else:
        conclusion = "success" if verdict == "passed" else "failure"
        final_status = "completed"
        title = {"passed": "Incident browser scenario passed", "failed": "Browser evidence did not match", "inconclusive": "Browser run was inconclusive"}[verdict]
    incident = result.get("incident") or {}
    lines = [
        reason,
        "",
        f"Tested commit: `{head_sha}`",
        f"Scenario: `{scenario_hash}`",
        f"Incident: {incident.get('reference') or incident.get('id') or 'not supplied'}",
        "",
        "| Target | Check | Expected | Observed |",
        "| --- | --- | --- | --- |",
    ]
    for check in result.get("checks") or []:
        target = check.get("target") or "preview"
        label = check.get("rowText") or check.get("path") or check.get("id") or ""
        expected = check.get("expectedBadge")
        observed = check.get("observedBadge")
        if "expectedStatus" in check:
            expected = f"HTTP {check.get('expectedStatus')}"
            observed_status = check.get("observedStatus")
            observed = f"HTTP {observed_status}" if observed_status is not None else "not observed"
        lines.append(f"| {target} | {label} | {expected or ''} | {observed or 'not observed'} |")
    _update_check(installation_id, repo, int(check_id), {
        "status": "completed", "conclusion": conclusion, "details_url": workflow_url,
        "output": {"title": title, "summary": "\n".join(lines)[:65000]},
    })
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE browser_test_runs SET status=%s,verdict=%s,result=%s,
                      workflow_run_id=%s,completed_at=NOW() WHERE id=%s""",
            (final_status, verdict, json.dumps(result), workflow_run_id, run_id),
        )
        conn.commit()
    return final_status
