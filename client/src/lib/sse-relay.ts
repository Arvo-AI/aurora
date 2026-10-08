/**
 * Relay a backend SSE body. Also cancels it when enqueue fails or the
 * consumer drops the stream — request.signal never aborts on Bun < 1.4
 * because node:http omits "close" on client disconnect (#703).
 */
export function relayServerSentEvents(
  backendBody: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): Response {
  const relay = createRelay(backendBody);

  const stream = new ReadableStream<Uint8Array>({
    async start(ctrl) {
      // addEventListener does not replay an abort that already happened.
      if (relay.stopped || signal.aborted) {
        relay.stop();
        closeQuietly(ctrl);
        return;
      }

      const reader = backendBody.getReader();
      relay.attach(reader);
      const heartbeat = new TextEncoder().encode(':heartbeat\n\n');
      const interval = setInterval(() => {
        try {
          ctrl.enqueue(heartbeat);
        } catch {
          // Client left. Signal abort is not guaranteed on older Bun.
          relay.stop();
        }
      }, 30_000);

      const onAbort = () => {
        relay.stop();
        closeQuietly(ctrl);
      };
      signal.addEventListener('abort', onAbort);

      try {
        await copyUpstream(reader, ctrl, signal, relay);
      } finally {
        signal.removeEventListener('abort', onAbort);
        clearInterval(interval);
        relay.releaseIfRunning();
      }
    },
    cancel() {
      relay.stop();
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

interface Relay {
  stopped: boolean;
  stop: () => void;
  attach: (reader: ReadableStreamDefaultReader<Uint8Array>) => void;
  releaseIfRunning: () => void;
}

function createRelay(backendBody: ReadableStream<Uint8Array>): Relay {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let stopped = false;

  return {
    get stopped() {
      return stopped;
    },
    attach(next) {
      reader = next;
    },
    stop() {
      // Abort, cancel, and a failed enqueue can all fire for one disconnect.
      if (stopped) return;
      stopped = true;
      // A live reader locks the body; cancelling the body itself throws.
      if (reader) {
        reader.cancel().catch(() => {});
        return;
      }
      backendBody.cancel().catch(() => {});
    },
    releaseIfRunning() {
      // cancel() already released the lock; release only on a clean end.
      if (stopped || !reader) return;
      try { reader.releaseLock(); } catch { /* already released */ }
    },
  };
}

async function copyUpstream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  ctrl: ReadableStreamDefaultController<Uint8Array>,
  signal: AbortSignal,
  relay: Relay,
) {
  try {
    while (!relay.stopped) {
      const { done, value } = await reader.read();
      // Upstream ended, or a disconnect cancelled this read.
      if (done || relay.stopped) break;
      try {
        ctrl.enqueue(value);
      } catch {
        // Consumer stopped pulling. Drop the backend socket too.
        relay.stop();
        break;
      }
    }
    if (!relay.stopped) closeQuietly(ctrl);
  } catch (err) {
    relay.stop();
    // Abort already closed the controller; don't surface that as an error.
    if (!signal.aborted) errorQuietly(ctrl, err);
  }
}

function closeQuietly(ctrl: ReadableStreamDefaultController<Uint8Array>) {
  try { ctrl.close(); } catch { /* already closed or errored */ }
}

function errorQuietly(ctrl: ReadableStreamDefaultController<Uint8Array>, err: unknown) {
  try { ctrl.error(err); } catch { /* already closed */ }
}
