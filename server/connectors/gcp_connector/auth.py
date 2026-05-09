import os
from dotenv import load_dotenv
import logging
from googleapiclient import discovery as _gapid
import warnings

# Load environment variables from .env
load_dotenv()

logging.basicConfig(level=logging.DEBUG)

logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# Silence any remaining ResourceWarning traces (typically dangling SSL sockets)
warnings.filterwarnings("ignore", category=ResourceWarning)

def _read_env(name: str) -> str | None:
    """Read ``name`` from env, stripping surrounding quotes + whitespace.

    Some operator .env files wrap OAuth credentials in literal quotes
    (e.g. ``CLIENT_ID="..."``). Most loaders strip them, but Compose v1,
    raw ``export``, and some interactive shells preserve them, breaking
    Google's authorize endpoint with ``Missing required parameter:
    client_id``. Strip defensively.
    """
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value or None


CLIENT_ID = _read_env("CLIENT_ID")
CLIENT_SECRET = _read_env("CLIENT_SECRET")

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
# Required OAuth scopes
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
OPENID_SCOPE = "openid"
# Base scope string kept for backward-compatibility
AUTH_SCOPE = CLOUD_PLATFORM_SCOPE

# ---------------------------------------------------------------------------
# Google API client tweaks
# ---------------------------------------------------------------------------

if not getattr(_gapid, "_aurora_cache_patch", False):
    _orig_build = _gapid.build

    _service_cache: dict[tuple[str, str, int | None], object] = {}

    def _build_with_no_cache(serviceName, version, *args, **kwargs):  # type: ignore[override]
        """Wrapper around googleapiclient.discovery.build.

        * Forces cache_discovery=False to avoid deprecated on-disk caches.
        * Caches the returned service object in-memory keyed by (service, version, id(credentials)).
          This prevents a fresh discovery-document download on every call which was
          creating dozens of short-lived SSL sockets and the associated ResourceWarnings.
        """
        kwargs.setdefault("cache_discovery", False)
        creds = kwargs.get("credentials")
        key = (serviceName, version, id(creds) if creds is not None else None)

        svc = _service_cache.get(key)
        if svc is not None:
            return svc

        svc = _orig_build(serviceName, version, *args, **kwargs)
        _service_cache[key] = svc
        return svc

    _gapid.build = _build_with_no_cache  # type: ignore[assignment]
    _gapid._aurora_cache_patch = True  # sentinel so we patch only once
