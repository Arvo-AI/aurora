"""What a stored state depends on (log templates, cleaned spec values) is pinned to
STATE_VERSION, so a change to the masking cannot ship without a bump."""

import hashlib
import json

from services.watch.detector import STATE_VERSION, template_counts
from services.watch.snapshot import _clean

PW = "change" + "me"  # synthetic credentials, split so secret scanners see no literal
KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"
LOG_LINES = [
    '10.0.0.7 - - [17/Nov/2026:21:44:51 +0000] "GET /api/orders?access_token=Zk3pQ9' + 'vX2mT8 HTTP/1.1" 503 12',
    '{"time":"2026-11-18T05:00:00Z","uri":"/api/orders?page_token=abc123","status":503,"error":"upstream timed out"}',
    'level=error msg="upstream call failed" status=503 session_id=a1b2c3d4e5f6a7b8c9d0e1f2',
    f"ERROR request failed: 401 Unauthorized api_key={KEY}",
    f"ERROR failed to connect: config {{User:shop Password:{PW} DB:0}}",
    f"error: auth failed for user shop, password: {PW}",
    "ERROR request failed: token: expired",
    f"ERROR POST /login 401 credentials=bob:{PW}",
    'level=error msg="auth" Authorization: Bearer abc.def.ghi',
    "ERROR request failed: redis error: connection refused",
    'ERROR Token refresh failed: I/O error on POST request for "http://auth/oauth/token": Connection refused',
    "ERROR job ckx7q2m9a0001ab3cd4ef5gh attempt 2 failed",
]
SPEC_VALUES = [
    f"https://hooks.example.com/notify?credentials=robot:{PW}",
    "https://api.example.com/v1/feed?apikey=Xy7?k=9LpQ2",
    f"postgres://shop:{PW}@db:5432/shop?sslmode=require",
    "--token=abc?x=1",
    f"https://b.s3.amazonaws.com/o?X-Amz-Credential={KEY}%2F20261118&X-Amz-Signature=9f8e",
]
# The digest of the corpus above under each STATE_VERSION's rules. A failure here means
# the masking changed: bump STATE_VERSION and record the new digest under it.
GOLDEN = {3: "8f10a7d67246138e"}


def masking_digest() -> str:
    templates = [sorted(template_counts([line])) for line in LOG_LINES]
    cleaned = [_clean(v, digest=False) for v in SPEC_VALUES]
    return hashlib.sha256(json.dumps([templates, cleaned]).encode()).hexdigest()[:16]


def test_the_masking_a_state_was_made_with_is_its_version():
    assert GOLDEN.get(STATE_VERSION) == masking_digest(), (
        "log templating or spec cleaning changed: bump STATE_VERSION and record the digest")
