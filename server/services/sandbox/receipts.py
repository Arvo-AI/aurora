"""Expiring, signed public diagnoses: no per-worker session store or client trust."""

import base64
import hashlib
import hmac
import json
import os
import time

from pydantic import Field

from .evidence import load_case
from .models import CaseId, Diagnosis, StrictModel

TTL_SECONDS = 3600
_DOMAIN = b"aurora-sandbox-run-v1\0"


class RunExpired(ValueError):
    pass


class InvalidRun(ValueError):
    pass


class RunReceipt(StrictModel):
    version: int = 1
    case_id: CaseId
    run_id: str = Field(min_length=1, max_length=80)
    issued_at: int
    evidence_sha256: str
    diagnosis: Diagnosis


def evidence_digest(case):
    payload = json.dumps(case.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _encode(payload):
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _key():
    secret = os.getenv("INTERNAL_API_SECRET", "")
    if not secret:
        raise InvalidRun("Sandbox server configuration is incomplete.")
    return secret.encode()


def issue_receipt(result):
    # Only public sample findings are included. The signing secret is never sent.
    receipt = RunReceipt(
        case_id=result["case_id"], run_id=result["run_id"], issued_at=int(time.time()),
        evidence_sha256=result["evidence_sha256"], diagnosis=result["diagnosis"],
    )
    payload = _encode(receipt.model_dump_json().encode())
    signature = _encode(hmac.digest(_key(), _DOMAIN + payload.encode(), "sha256"))
    return f"{payload}.{signature}"


def verify_receipt(token):
    try:
        if len(token) > 60000:
            raise ValueError()
        payload, signature = token.split(".")
        expected = _encode(hmac.digest(_key(), _DOMAIN + payload.encode("ascii"), "sha256"))
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise ValueError()
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        receipt = RunReceipt.model_validate_json(decoded)
        if receipt.version != 1 or receipt.issued_at > time.time() + 30:
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise InvalidRun("This investigation could not be verified. Run a new comparison to challenge it.") from exc
    if time.time() - receipt.issued_at > TTL_SECONDS:
        raise RunExpired("This investigation has expired. Run a new comparison to challenge it.")
    if not hmac.compare_digest(receipt.evidence_sha256, evidence_digest(load_case(receipt.case_id))):
        raise RunExpired("The sample evidence has changed. Run a new comparison to challenge it.")
    return receipt
