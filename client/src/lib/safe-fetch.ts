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

  const timer = setTimeout(() => controller.abort(), timeoutMs);

  // Strip our additions from `init` before spreading so we don't leak `timeoutMs`.
  const { timeoutMs: _ignored, signal: _ignoredSignal, ...rest } = init ?? {};
  void _ignored;
  void _ignoredSignal;

  try {
    const fetchPromise = fetch(input, { ...rest, signal: controller.signal });
    const timeoutPromise = new Promise<never>((_, reject) => {
      // Tiny offset so the AbortController fires first on well-behaved fetches;
      // this Promise.race is the hard guarantee for stuck Bun sockets.
      setTimeout(
        () => reject(new Error(`safeFetch timeout after ${timeoutMs}ms`)),
        timeoutMs + 100,
      );
    });
    return await Promise.race([fetchPromise, timeoutPromise]);
  } finally {
    clearTimeout(timer);
  }
}

/** Convenience type-guard for the timeout error thrown by safeFetch. */
export function isSafeFetchTimeout(err: unknown): boolean {
  if (err instanceof Error) {
    if (err.name === 'AbortError') return true;
    if (err.name === 'TimeoutError') return true;
    if (err.message.startsWith('safeFetch timeout after')) return true;
  }
  return false;
}
