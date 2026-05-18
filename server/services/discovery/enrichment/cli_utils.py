"""
Shared CLI utilities for discovery enrichment modules.

Provides a common subprocess runner for CLI commands that return JSON output.
Used by AWS, Azure, Kubernetes, and serverless enrichment modules to avoid
duplicating the same subprocess/JSON-parsing boilerplate.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120


def run_cli_json_command(cmd, env, timeout=DEFAULT_TIMEOUT, label="cli"):
    """Run a CLI command and return parsed JSON output.

    Args:
        cmd: List of command arguments.
        env: Explicit environment dict for subprocess. Must not be None —
             callers are required to pass a minimal isolated env to prevent
             the subprocess from inheriting the full server environment.
        timeout: Command timeout in seconds (default 120).
        label: A safe, caller-provided string used in log messages (never
               derived from ``cmd`` to avoid leaking credentials).

    Returns:
        Parsed JSON output, or None on failure.
    """
    if env is None:
        logger.error("[%s] Missing explicit subprocess env — refusing to inherit server environment", label)
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "[%s] CLI command failed (exit %d): %s",
                label,
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("[%s] CLI command timed out after %ds", label, timeout)
        return None
    except json.JSONDecodeError as e:
        logger.warning("[%s] Failed to parse CLI JSON output: %s", label, e)
        return None
    except FileNotFoundError:
        logger.error("[%s] CLI tool not found", label)
        return None


def run_cli_command(cmd, env, timeout=DEFAULT_TIMEOUT, label="cli"):
    """Run a CLI command and return (stdout_string, error_string_or_None).

    Unlike run_cli_json_command, this returns raw stdout without JSON parsing.
    Used by kubernetes_enrichment for credential commands that may not return JSON.

    Args:
        cmd: List of command arguments.
        env: Explicit environment dict for subprocess. Must not be None —
             callers are required to pass a minimal isolated env to prevent
             the subprocess from inheriting the full server environment.
        timeout: Command timeout in seconds (default 120).
        label: A safe, caller-provided string used in error messages (never
               derived from ``cmd`` to avoid leaking credentials).

    Returns:
        Tuple of (stdout_str, error_str_or_None).
    """
    if env is None:
        return None, f"[{label}] Missing explicit subprocess env — refusing to inherit server environment"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return None, f"[{label}] Command failed (rc={result.returncode}): {stderr}"
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, f"[{label}] Command timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"[{label}] Command error: {e}"
