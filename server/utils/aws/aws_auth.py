"""Utility helpers for AWS authentication.

This module centralises all logic around assuming an AWS IAM role via STS and
returning temporary credentials in a unified dict as well as a ready-to-use
``boto3.Session`` instance.

Other modules can simply do::

    from utils.aws.aws_auth import assume_role_and_get_creds

    creds, session = assume_role_and_get_creds(role_arn, external_id)

The returned ``creds`` dictionary is shaped identically to the ``Credentials``
section of ``sts.assume_role`` so it can drop-in replace existing code that was
previously reading that structure directly.
"""

from __future__ import annotations

import logging
import boto3
from typing import Dict, Tuple, Optional
from datetime import timedelta, datetime, timezone

logger = logging.getLogger(__name__)

# Default session duration – 1 hour (minimum) – can be overridden.
_DEFAULT_DURATION = 3600


def assume_role_and_get_creds(
    role_arn: str,
    external_id: Optional[str] = None,
    *,
    session_name: Optional[str] = None,
    duration_seconds: int | None = None,
) -> Tuple[Dict[str, str], boto3.Session]:
    """Assume *role_arn* and return temporary credentials + session.

    Parameters
    ----------
    role_arn:
        The full ARN of the IAM role to assume.
    external_id:
        External ID string if the role requires it (can be *None*).
    session_name:
        Custom session name.  When *None* a default based on current timestamp
        is generated.
    duration_seconds:
        Optional session duration.  Falls back to 3600 seconds.
        var or 3600 seconds.

    Returns
    -------
    Tuple[dict, boto3.Session]
        1. A dict with the standard temporary credential keys – ``AccessKeyId``,
           ``SecretAccessKey``, ``SessionToken``, ``Expiration`` – plus a helper
           field ``expires_at`` (POSIX seconds).
        2. A ``boto3.Session`` initialised with those credentials ready for API
           calls.
    """
    if not role_arn:
        raise ValueError("role_arn is required")

    sts_client = boto3.client("sts")

    assume_kwargs: Dict[str, object] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name or f"aurora-session-{int(datetime.now().timestamp())}",
        "DurationSeconds": duration_seconds or _DEFAULT_DURATION,
    }
    if external_id:
        assume_kwargs["ExternalId"] = external_id

    logger.debug("Assuming role with kwargs: %s", {k: v for k, v in assume_kwargs.items() if k != "ExternalId"})

    resp = sts_client.assume_role(**assume_kwargs)
    creds = resp["Credentials"]

    # Enrich credentials with convenience timestamp for easier expiry checks.
    expiration: datetime = creds["Expiration"]
    creds_dict = {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
        "expires_at": int(expiration.replace(tzinfo=timezone.utc).timestamp()),
    }

    session = boto3.Session(
        aws_access_key_id=creds_dict["aws_access_key_id"],
        aws_secret_access_key=creds_dict["aws_secret_access_key"],
        aws_session_token=creds_dict["aws_session_token"],
    )

    logger.info(
        "Assumed role %s, session expires at %s (in %s)",
        role_arn,
        expiration.isoformat(),
        expiration - datetime.utcnow().replace(tzinfo=timezone.utc),
    )

    return creds_dict, session
