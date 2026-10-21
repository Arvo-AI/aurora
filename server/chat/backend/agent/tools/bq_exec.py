"""Argv, caps, and cancel for `bq` commands run by cloud_exec."""

import logging
import shlex
import uuid
from typing import NamedTuple, Optional, Tuple

from utils.terminal.terminal_run import terminal_run

logger = logging.getLogger(__name__)

_BQ_QUERY_TIMEOUT_SECONDS = 300
# Applied only when the command does not set --maximum_bytes_billed.
_BQ_DEFAULT_BYTES_BILLED = 10_000_000_000
# 1 TiB. An explicit flag cannot go past the on-demand price of one tebibyte.
_BQ_BYTES_BILLED_CEILING = 1024 ** 4
_BQ_DEFAULT_MAX_ROWS = 100
# Past this the output trim drops the extra rows before the model sees them.
_BQ_MAX_ROWS_CEILING = 1000

# Verbs bq accepts. A flag value such as `--location US` must not be treated as one.
_BQ_VERBS = frozenset({
    "query", "wait", "ls", "show", "head", "mk", "rm", "cp", "load",
    "extract", "cancel", "help", "version", "init", "update", "shell",
    "partition", "insert",
})


def _bq_verb_index(tokens: list) -> Optional[int]:
    """Index of the bq verb, or None when tokens are not a bq command."""
    if not tokens or tokens[0] != "bq":
        return None
    for i, token in enumerate(tokens):
        if i > 0 and token in _BQ_VERBS:
            return i
    return None


def _bq_subcommand(command: str) -> Optional[str]:
    """The bq verb. None when the command is not bq or the verb is unknown."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = _bq_verb_index(tokens)
    if index is None:
        return None
    return tokens[index]


def bq_command_timeout(command: str) -> Optional[int]:
    """300s for query and wait. None for every other command."""
    # Match the verb, not the SQL, so "created_at" does not change the limit
    if _bq_subcommand(command) in ("query", "wait"):
        return _BQ_QUERY_TIMEOUT_SECONDS
    return None


def _redact_oauth_access_token(argv: list) -> str:
    """Join argv for logs. The access token never appears in the result."""
    redacted = []
    skip_value = False
    for token in argv:
        if skip_value:
            redacted.append("[REDACTED]")
            skip_value = False
            continue
        if token == "--oauth_access_token":
            redacted.append(token)
            skip_value = True
            continue
        if token.startswith("--oauth_access_token="):
            redacted.append("--oauth_access_token=[REDACTED]")
            continue
        redacted.append(token)
    return " ".join(shlex.quote(part) for part in redacted)


def _argv_has_flag(argv: list, name: str) -> bool:
    return any(token == name or token.startswith(name + "=") for token in argv)


def bq_query_location(argv: list) -> Optional[str]:
    """The --location the query was sent to, so cancel hits the same region."""
    return _flag_value(argv, "--location")


def bq_query_project(argv: list, default: Optional[str] = None) -> Optional[str]:
    """Project the query ran in. An explicit --project_id wins over the default."""
    return _flag_value(argv, "--project_id") or default


def _flag_value(argv: list, name: str) -> Optional[str]:
    """Value of --name=value or --name value. None when the flag is absent."""
    for i, token in enumerate(argv):
        if token == name:
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
            return None
        prefix = name + "="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _replace_flag_value(argv: list, name: str, new_value: str) -> None:
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            argv[i + 1] = new_value
            return
        if token.startswith(name + "="):
            argv[i] = f"{name}={new_value}"
            return


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _bounded_int_flag(argv: list, name: str, default: int, ceiling: int) -> str:
    """Flag token to insert, or '' when the command already has one.

    A value above the ceiling is rewritten in place.
    """
    if not _argv_has_flag(argv, name):
        return f"{name}={default}"
    parsed = _parse_int(_flag_value(argv, name))
    # Leave a non-numeric value for bq to reject
    if parsed is not None and parsed > ceiling:
        _replace_flag_value(argv, name, str(ceiling))
    return ""


def _query_limit_flags(argv: list) -> list:
    """Caps to insert. Empty when the command already sets that flag."""
    flags = []
    # Standard SQL unless the command already picked a dialect
    if not _argv_has_flag(argv, "--nouse_legacy_sql") and not _argv_has_flag(argv, "--use_legacy_sql"):
        flags.append("--nouse_legacy_sql")
    for flag in (
        _bounded_int_flag(argv, "--maximum_bytes_billed", _BQ_DEFAULT_BYTES_BILLED, _BQ_BYTES_BILLED_CEILING),
        _bounded_int_flag(argv, "--max_rows", _BQ_DEFAULT_MAX_ROWS, _BQ_MAX_ROWS_CEILING),
    ):
        if flag:
            flags.append(flag)
    return flags


def _assign_job_id(argv: list) -> Tuple[str, str]:
    """Return the job id and a flag to insert, or '' when one is already set."""
    job_id = _flag_value(argv, "--job_id")
    if job_id:
        return job_id, ""
    job_id = f"aurora-{uuid.uuid4().hex}"
    # An empty --job_id still occupies the flag, so replace it instead of adding another
    if _argv_has_flag(argv, "--job_id"):
        _replace_flag_value(argv, "--job_id", job_id)
        return job_id, ""
    return job_id, f"--job_id={job_id}"


def _apply_bq_query_defaults(argv: list) -> str:
    """Fill query caps and return the job id cancel uses if the CLI is killed."""
    query_idx = _bq_verb_index(argv)
    # Global flags belong before the verb
    if not _argv_has_flag(argv, "--format"):
        argv.insert(query_idx, "--format=json")
        query_idx += 1
    flags = _query_limit_flags(argv)
    job_id, job_flag = _assign_job_id(argv)
    if job_flag:
        flags.append(job_flag)
    for offset, flag in enumerate(flags):
        argv.insert(query_idx + 1 + offset, flag)
    return job_id


class BqExec(NamedTuple):
    argv: list
    logged: str
    job_id: Optional[str]


def prepare_bq_exec(
    command: str,
    access_token: Optional[str],
    project_id: Optional[str],
) -> BqExec:
    """Argv for `bq`, a log-safe rendering, and the query job id.

    The access token is placed only on the argv. Callers must log `logged`,
    not the argv. Query caps are filled in when missing and clamped when too high.
    """
    argv = shlex.split(command)
    if not argv or argv[0] != "bq":
        return BqExec(argv, _redact_oauth_access_token(argv), None)

    insert_at = 1
    if project_id and not _argv_has_flag(argv, "--project_id"):
        argv.insert(insert_at, f"--project_id={project_id}")
        insert_at += 1
    if access_token and not _argv_has_flag(argv, "--oauth_access_token"):
        argv.insert(insert_at, f"--oauth_access_token={access_token}")

    query_idx = _bq_verb_index(argv)
    # Caps apply to queries. ls and show stay as the command wrote them.
    if query_idx is None or argv[query_idx] != "query":
        return BqExec(argv, _redact_oauth_access_token(argv), None)
    # Apply first. The log string has to be the argv that actually runs.
    job_id = _apply_bq_query_defaults(argv)
    return BqExec(argv, _redact_oauth_access_token(argv), job_id)


def cancel_bq_job(
    job_id: str,
    project_id: Optional[str],
    location: Optional[str],
    access_token: Optional[str],
    env: dict,
) -> bool:
    """Stop a query job we started. Direct exec, because Ask mode blocks cancel."""
    # None inherits the server environment, including any ambient credentials
    if not isinstance(env, dict):
        logger.warning("bq cancel skipped for job %s: no isolated env", job_id)
        return False
    argv = ["bq"]
    if project_id:
        argv.append(f"--project_id={project_id}")
    if location:
        argv.append(f"--location={location}")
    # Default cancel waits until the job is DONE, which can outlast this call
    argv.extend(["--synchronous_mode=false", "cancel", job_id])
    try:
        result = terminal_run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            trusted=True,
            oauth_access_token=access_token,
        )
    except Exception:
        logger.warning("bq cancel failed for job %s", job_id)
        return False
    if result.returncode != 0:
        logger.warning("bq cancel exited %s for job %s", result.returncode, job_id)
        return False
    return True


def explain_bq_timeout(
    result,
    *,
    job_id: Optional[str],
    project_id: Optional[str],
    location: Optional[str],
    access_token: Optional[str],
    env: dict,
    timeout_s: int,
):
    """Cancel the job behind a killed query and say so in stderr."""
    if result.returncode != 124 or not job_id:
        return result
    cancelled = cancel_bq_job(job_id, project_id, location, access_token, env)
    # --synchronous_mode=false returns before the job reaches DONE
    if cancelled:
        result.stderr = (
            f"Query exceeded the {timeout_s}s timeout. "
            "Cancellation of the BigQuery job was requested."
        )
    else:
        result.stderr = (
            f"Query exceeded the {timeout_s}s timeout. "
            "Cancelling the BigQuery job failed; it may still be running."
        )
    return result
