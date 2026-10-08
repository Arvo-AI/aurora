/**
 * Relay a backend SSE body. Also cancels it when enqueue fails or the
 * consumer drops the stream — request.signal never aborts on Bun < 1.4
 * because node:http omits "close" on client disconnect (#703).
 */
export function relayServerSentEvents(
  backendBody: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): Response {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let interval: ReturnType<typeof setInterval> | undefined;
  let stopped = false;

  const stopUpstream = () => {
    // Abort, cancel, and a failed enqueue can all fire for one disconnect.
    if (stopped) return;
    stopped = true;
    if (interval) clearInterval(interval);
    // A live reader locks the body; cancelling the body itself throws.
    if (reader) {
      reader.cancel().catch(() => {});
      return;
    }
    backendBody.cancel().catch(() => {});
  };

  const stream = new ReadableStream<Uint8Array>({
    async start(ctrl) {
      // Already aborted, or the consumer cancelled before the loop started.
      // addEventListener does not replay an abort that already happened.
      if (stopped || signal.aborted) {
        stopUpstream();
        try { ctrl.close(); } catch { /* already closed */ }
        return;
      }

      reader = backendBody.getReader();
      const heartbeat = new TextEncoder().encode(':heartbeat\n\n');

      interval = setInterval(() => {
        try {
          ctrl.enqueue(heartbeat);
        } catch {
          // Client left. Signal abort is not guaranteed on older Bun.
          stopUpstream();
        }
      }, 30_000);

      const onAbort = () => {
        stopUpstream();
        try { ctrl.close(); } catch { /* already closed or errored */ }
      };
      signal.addEventListener('abort', onAbort);

      try {
        while (!stopped) {
          const { done, value } = await reader.read();
          // Upstream ended, or a disconnect cancelled this read.
          if (done || stopped) break;
          try {
            ctrl.enqueue(value);
          } catch {
            // Consumer stopped pulling. Drop the backend socket too.
            stopUpstream();
            break;
          }
        }
        if (!stopped) {
          try { ctrl.close(); } catch { /* consumer already gone */ }
        }
      } catch (err) {
        stopUpstream();
        // Abort already closed the controller; don't surface that as an error.
        if (!signal.aborted) {
          try { ctrl.error(err); } catch { /* already closed */ }
        }
      } finally {
        signal.removeEventListener('abort', onAbort);
        if (interval) clearInterval(interval);
        // cancel() already released the lock; release only on a clean end.
        if (!stopped && reader) {
          try { reader.releaseLock(); } catch { /* already released */ }
        }
      }
    },
    cancel() {
      stopUpstream();
    },
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  });
}
