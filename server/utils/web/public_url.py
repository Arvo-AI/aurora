"""API base URL that a service outside this machine can reach."""

import os


def external_backend_url(backend_url: str | None = None) -> str:
    """Backend URL, with localhost swapped for NGROK_URL in local dev.

    ``backend_url`` overrides ``NEXT_PUBLIC_BACKEND_URL`` when a caller has
    already resolved a fallback (for example the incoming request host).
    """
    if backend_url is None:
        backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL") or ""
    backend_url = backend_url.rstrip("/")
    ngrok_url = (os.getenv("NGROK_URL") or "").rstrip("/")
    # An IdP or webhook sender cannot call the dev server on localhost.
    if ngrok_url and backend_url.startswith("http://localhost"):
        return ngrok_url
    return backend_url
