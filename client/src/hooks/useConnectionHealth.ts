"use client";

import { useEffect } from 'react';
import { safeFetch } from '@/lib/safe-fetch';

/**
 * Detects stale browser→server connections after idle periods.
 *
 * When the tab becomes visible after being hidden for >2 minutes, pings
 * /api/ping with a 3-second timeout. On failure, dispatches
 * "aurora:connection-stale" so SSE consumers can reconnect.
 */
export function useConnectionHealth() {
  useEffect(() => {
    let hiddenAt = 0;

    const onVisibilityChange = async () => {
      if (document.hidden) {
        hiddenAt = Date.now();
        return;
      }

      const hiddenDuration = hiddenAt ? Date.now() - hiddenAt : 0;
      hiddenAt = 0;

      if (hiddenDuration < 120_000) return;

      try {
        const res = await safeFetch('/api/ping', {
          cache: 'no-store',
          timeoutMs: 3_000,
        });
        if (!res.ok) {
          window.dispatchEvent(new Event('aurora:connection-stale'));
        }
      } catch {
        window.dispatchEvent(new Event('aurora:connection-stale'));
      }
    };

    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, []);
}
