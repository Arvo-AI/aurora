/**
 * safeFetch — Promise.race-based fetch with a hard timeout.
 *
 * Bun has known stale-socket hangs where AbortController alone is not reliable
 * (see project memory: feedback_abortcontroller_bun). Promise.race wins by
 * resolving as soon as either the fetch completes OR the timer fires, even if
 * the abort signal silently fails to propagate.
 *
 * Default timeout is 30s. Pass `timeoutMs` in init to override.
 */
export async function safeFetch(
  input: RequestInfo | URL,
  init?: RequestInit & { timeoutMs?: number },
): Promise<Response> {
  const timeoutMs = init?.timeoutMs ?? 30_000;

  // Honor a caller-supplied AbortSignal by chaining it into our controller.
  const controller = new AbortController();
  const externalSignal = init?.signal ?? null;
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', () => controller.abort(), { once: true });
    }
  }

  // Track whether OUR timer fired so an AbortError from the underlying fetch
  // can be reclassified as a timeout (vs. a caller-cancel). Without this the
  // Promise.race is genuinely racy — sometimes the timeout message wins,
  // sometimes the fetch's AbortError lands first — and downstream isSafeFetchTimeout
  // misclassifies the latter.
  let didTimeOut = false;
  const timer = setTimeout(() => {
    didTimeOut = true;
    controller.abort();
  }, timeoutMs);
  let innerTimer: ReturnType<typeof setTimeout> | undefined;

  // Strip our additions from `init` before spreading so we don't leak `timeoutMs`.
  const { timeoutMs: _ignored, signal: _ignoredSignal, ...rest } = init ?? {};
  void _ignored;
  void _ignoredSignal;

  try {
    const fetchPromise = fetch(input, { ...rest, signal: controller.signal }).catch(
      (err: unknown) => {
        if (didTimeOut && err instanceof Error && err.name === 'AbortError') {
          throw new Error(`safeFetch timeout after ${timeoutMs}ms`);
        }
        throw err;
      },
    );
    const timeoutPromise = new Promise<never>((_, reject) => {
      // Tiny offset so the AbortController fires first on well-behaved fetches;
      // this Promise.race is the hard guarantee for stuck Bun sockets.
      innerTimer = setTimeout(
        () => reject(new Error(`safeFetch timeout after ${timeoutMs}ms`)),
        timeoutMs + 100,
      );
    });
    return await Promise.race([fetchPromise, timeoutPromise]);
  } finally {
    clearTimeout(timer);
    if (innerTimer !== undefined) clearTimeout(innerTimer);
  }
}

/** Convenience type-guard for the timeout error thrown by safeFetch.
 *  Note: AbortError on its own is no longer treated as a timeout — a caller-
 *  supplied AbortSignal can also produce AbortError, and treating that as a
 *  timeout would mask the real cancel. Only the explicit "safeFetch timeout"
 *  message and the platform TimeoutError DOMException count.
 */
export function isSafeFetchTimeout(err: unknown): boolean {
  if (err instanceof Error) {
    if (err.name === 'TimeoutError') return true;
    if (err.message.startsWith('safeFetch timeout after')) return true;
  }
  return false;
}
