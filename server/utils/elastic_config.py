"""Shared Elastic configuration helpers."""

import os


def parse_elastic_ssl_verify():
    """Parse ELASTIC_SSL_VERIFY env var for the ``requests`` *verify* parameter.

    Defaults to ``true`` (Elastic Cloud always serves valid public certs).
    Accepts ``false``/``true`` or a path to a CA bundle for self-managed clusters.
    """
    raw = os.environ.get("ELASTIC_SSL_VERIFY", "true")
    lowered = raw.strip().lower()
    if lowered in ("0", "false", "no"):
        return False
    if lowered in ("1", "true", "yes", ""):
        return True
    return raw


ELASTIC_SSL_VERIFY = parse_elastic_ssl_verify()
