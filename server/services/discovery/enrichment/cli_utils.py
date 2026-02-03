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


def run_cli_json_command(cmd, env=None, timeout=DEFAULT_TIMEOUT):
    """Run a CLI command and return parsed JSON output.

    Args:
        cmd: List of command arguments.
        env: Optional environment dict for subprocess.
        timeout: Command timeout in seconds (default 120).

    Returns:
        Parsed JSON output, or None on failure.
    """
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
                "CLI command failed (exit %d): %s — cmd: %s",
                result.returncode,
                result.stderr.strip(),
                " ".join(cmd),
            )
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("CLI command timed out after %ds: %s", timeout, " ".join(cmd))
        return None
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse CLI JSON output: %s", e)
        return None
    except FileNotFoundError:
        logger.error("CLI tool not found for command: %s", cmd[0])
        return None


def run_cli_command(cmd, env=None, timeout=DEFAULT_TIMEOUT):
    """Run a CLI command and return (stdout_string, error_string_or_None).

    Unlike run_cli_json_command, this returns raw stdout without JSON parsing.
    Used by kubernetes_enrichment for credential commands that may not return JSON.

    Args:
        cmd: List of command arguments.
        env: Optional environment dict for subprocess.
        timeout: Command timeout in seconds (default 120).

    Returns:
        Tuple of (stdout_str, error_str_or_None).
    """
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
            return None, f"Command failed (rc={result.returncode}): {stderr}"
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return None, f"Command error: {e}"
