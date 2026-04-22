"""Shared Splunk configuration helpers."""

import os


def parse_splunk_ssl_verify():
    """Parse SPLUNK_SSL_VERIFY env var for the ``requests`` *verify* parameter."""
    raw = os.environ.get("SPLUNK_SSL_VERIFY", "false")
    lowered = raw.strip().lower()
    if lowered in ("0", "false", "no", ""):
        return False
    if lowered in ("1", "true", "yes"):
        return True
    return raw


SPLUNK_SSL_VERIFY = parse_splunk_ssl_verify()
