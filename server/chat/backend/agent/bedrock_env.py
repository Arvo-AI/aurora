"""Bedrock region and credential resolution shared by every boto3 client Aurora builds.

``BedrockProvider`` (chat models through ``ChatBedrockConverse``) and the alert
correlation ``EmbeddingClient`` (``bedrock-runtime`` ``InvokeModel`` for Titan
embeddings) must agree on three things, so they live here once:

- which region to talk to (``BEDROCK_REGION`` -> ``AWS_REGION`` -> ``AWS_DEFAULT_REGION``);
- that ``BEDROCK_REGION=global`` names Bedrock's ``global.`` inference-profile geo,
  not an AWS region — boto3 still has to sign against a real region;
- which credentials to pass explicitly (``BEDROCK_*`` first, then the standard
  ``AWS_*`` variables) and when to pass none at all so boto3's default chain —
  EC2 instance profile, ECS task role, EKS IRSA, shared config — is used. Static
  keys are the fallback, never a requirement.

Stdlib only: imported by modules that must stay loadable without boto3.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from chat.backend.agent.call_options import BEDROCK_GLOBAL_GEO

# boto3 needs a real region to sign against when the ``global.`` profile geo is in use
# and no AWS_* region is configured. Mirrors the historical provider fallback.
GLOBAL_HOME_REGION_FALLBACK = "us-east-1"


def resolve_bedrock_region() -> Optional[str]:
    """Configured Bedrock region / profile geo, ``BEDROCK_*`` before ``AWS_*``.

    Returns ``None`` when nothing is set (the provider is then unavailable in
    native mode; callers decide their own fallback).
    """
    for var in ("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        value = (os.getenv(var) or "").strip()
        if value:
            return value
    return None


def resolve_client_region(region: Optional[str]) -> Optional[str]:
    """Real AWS region for boto3, given a possibly-``global`` profile geo.

    ``global`` is not an AWS region: passing it to boto3 produces an unresolvable
    endpoint. Fall back to the standard AWS region vars, then to us-east-1, so
    ``BEDROCK_REGION=global`` is actually a configuration the clients support —
    and so pricing (which treats ``global`` as premium-free) matches routing.
    Any other value (including ``None``) is returned unchanged.
    """
    if (region or "").lower() != BEDROCK_GLOBAL_GEO:
        return region
    for candidate in (os.getenv("AWS_REGION"), os.getenv("AWS_DEFAULT_REGION")):
        if candidate and candidate.lower() != BEDROCK_GLOBAL_GEO:
            return candidate
    return GLOBAL_HOME_REGION_FALLBACK


def bedrock_profile_name() -> Optional[str]:
    """``BEDROCK_PROFILE`` (named ``~/.aws`` profile) or ``None``."""
    return (os.getenv("BEDROCK_PROFILE") or "").strip() or None


def bedrock_credential_kwargs() -> Dict[str, str]:
    """Explicit credential kwargs for ``boto3.client`` / ``ChatBedrockConverse``.

    Non-empty only when BOTH an access key id and a secret are configured
    (``BEDROCK_ACCESS_KEY_ID`` / ``BEDROCK_SECRET_ACCESS_KEY``, falling back to
    ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``), plus the matching session
    token when present. Otherwise ``{}``: boto3 resolves credentials through its
    default chain (IAM role / IRSA / shared config), which is the recommended path.
    Values are passed through to boto3 and never logged.
    """
    access_key = (os.getenv("BEDROCK_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (
        os.getenv("BEDROCK_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or ""
    ).strip()
    if not access_key or not secret_key:
        return {}
    kwargs = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key}
    session_token = (os.getenv("BEDROCK_SESSION_TOKEN") or os.getenv("AWS_SESSION_TOKEN") or "").strip()
    if session_token:
        kwargs["aws_session_token"] = session_token
    return kwargs
