"""Decide whether a repeat Grafana firing needs another RCA.

Metric samples and templated annotation text change on every evaluation of
the same alert, so they are not part of the identity compared here.
"""

import json


def _stable_alert_signature(title, service, severity, metadata) -> tuple:
    """Problem identity, ignoring metric samples and templated annotation text.

    Those change on every evaluation of the same alert.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    labels = metadata.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    label_items = tuple(
        sorted((str(key), "" if value is None else str(value)) for key, value in labels.items())
    )
    return (
        (title or "").strip(),
        (service or "").strip(),
        (severity or "").strip().lower(),
        label_items,
    )


def refire_needs_new_rca(previous_status, previous_signature, new_signature) -> bool:
    """Whether a repeat firing of an existing alert should start another RCA."""
    # Resolved incident firing again is a regression, not a duplicate delivery.
    if (previous_status or "").lower() == "resolved":
        return True
    # Title, service, severity, or labels changed under the same fingerprint.
    return previous_signature != new_signature
