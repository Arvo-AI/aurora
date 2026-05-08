"""Poll the target app until it responds, with exponential backoff."""
import sys
import time
import urllib.request
import urllib.error


def wait_for_app(url: str, timeout: int = 180) -> bool:
    """Block until the app at url responds with any 2xx/3xx, or timeout.

    Returns True if healthy, False if timed out.
    """
    start = time.time()
    interval = 2.0

    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status < 400:
                return True
        except urllib.error.HTTPError as e:
            # 3xx redirects (e.g. to /sign-in) mean the app is up
            if e.code < 500:
                return True
        except Exception:
            pass

        time.sleep(interval)
        interval = min(interval * 1.5, 10.0)

    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    print(f"Waiting for {args.url} (timeout: {args.timeout}s)...")
    if wait_for_app(args.url, args.timeout):
        print("App is ready.")
        sys.exit(0)
    else:
        print("Timed out waiting for app.", file=sys.stderr)
        sys.exit(1)
