"""Shared Splunk configuration helpers."""

import os


def parse_splunk_ssl_verify():
    """Parse SPLUNK_SSL_VERIFY into a value suitable for ``requests``' *verify* parameter.

    Returns ``True``, ``False``, or a string path to a CA bundle.
    """
    raw = os.environ.get("SPLUNK_SSL_VERIFY", "true")
    lowered = raw.strip().lower()
    if lowered in ("0", "false", "no"):
        return False
    if lowered in ("1", "true", "yes", ""):
        return True
    return raw


SPLUNK_SSL_VERIFY = parse_splunk_ssl_verify()
