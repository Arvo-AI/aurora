"""Shared RabbitMQ configuration helpers."""

import os


def parse_rabbitmq_ssl_verify():
    """Parse RABBITMQ_SSL_VERIFY env var for the ``requests`` *verify* parameter."""
    raw = os.environ.get("RABBITMQ_SSL_VERIFY", "false")
    lowered = raw.strip().lower()
    if lowered in ("0", "false", "no", ""):
        return False
    if lowered in ("1", "true", "yes"):
        return True
    return raw


RABBITMQ_SSL_VERIFY = parse_rabbitmq_ssl_verify()
