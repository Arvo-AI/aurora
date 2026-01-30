import { useEffect, useRef, useState, useCallback } from "react";
import {
  GRAPH_DISCOVERY_PROVIDERS,
  triggerGraphDiscovery,
  pollDiscoveryStatus,
} from "@/lib/services/graph-discovery";

export type GraphSyncStatus = "idle" | "building" | "synced" | "error";

const STORAGE_KEY = "aurora_graph_discovery_task";
const TRIGGER_KEY = "aurora_graph_discovery_trigger";
const POLL_INTERVAL = 5_000;
const SYNCED_DISPLAY_MS = 4_000;

interface StoredTask {
  taskId: string;
  userId: string;
  startedAt: number;
}

function getStoredTask(): StoredTask | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function setStoredTask(task: StoredTask) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(task));
}

function clearStoredTask() {
  localStorage.removeItem(STORAGE_KEY);
}

/**
 * Tracks graph discovery status for a connector card.
 *
 * Trigger: listens for the `providerStateChanged` custom event, which is
 * dispatched explicitly by connection flows (GCP OAuth callback, AWS
 * onboarding, OVH/Scaleway integration, etc.) — NOT by periodic status
 * checks.  The backend Redis dedup prevents duplicate concurrent tasks.
 *
 * All provider cards share a single task via localStorage.
 */
export function useGraphDiscoveryStatus(
  connectorId: string,
  isConnected: boolean,
  userId: string | null
): { syncStatus: GraphSyncStatus } {
  const [syncStatus, setSyncStatus] = useState<GraphSyncStatus>("idle");
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const syncedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const supportsDiscovery = (GRAPH_DISCOVERY_PROVIDERS as readonly string[]).includes(connectorId);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(
    (taskId: string, uid: string) => {
      setSyncStatus("building");
      stopPolling();

      const poll = async () => {
        try {
          const status = await pollDiscoveryStatus(uid, taskId);
          if (!status.complete) return;

          stopPolling();
          clearStoredTask();

          if (status.error) {
            setSyncStatus("error");
          } else {
            setSyncStatus("synced");
            syncedTimerRef.current = setTimeout(() => {
              setSyncStatus("idle");
            }, SYNCED_DISPLAY_MS);
          }
        } catch {
          // Network blip — keep polling
        }
      };

      poll();
      pollTimerRef.current = setInterval(poll, POLL_INTERVAL);
    },
    [stopPolling]
  );

  const triggerDiscovery = useCallback(
    async (uid: string) => {
      // If already polling, skip
      if (pollTimerRef.current) return;

      // If a task is stored, resume polling instead of triggering a new one
      const stored = getStoredTask();
      if (stored && stored.userId === uid) {
        startPolling(stored.taskId, uid);
        return;
      }

      try {
        const resp = await triggerGraphDiscovery(uid);
        // Backend returns status "already_running" if a task is active
        setStoredTask({ taskId: resp.task_id, userId: uid, startedAt: Date.now() });
        startPolling(resp.task_id, uid);
      } catch {
        setSyncStatus("error");
      }
    },
    [startPolling]
  );

  // Check trigger flag helper (used on mount and on providerStateChanged)
  const checkTrigger = useCallback(
    (uid: string) => {
      // Resume polling if a task is already in-flight
      const stored = getStoredTask();
      if (stored && stored.userId === uid) {
        startPolling(stored.taskId, uid);
        return;
      }

      // Check if a connection flow just completed (set by post-auth completion,
      // onboarding pages, etc.)
      const trigger = localStorage.getItem(TRIGGER_KEY);
      if (trigger) {
        localStorage.removeItem(TRIGGER_KEY);
        triggerDiscovery(uid);
      }
    },
    [startPolling, triggerDiscovery]
  );

  useEffect(() => {
    if (!supportsDiscovery || !isConnected || !userId) return;

    checkTrigger(userId);

    // Also listen for providerStateChanged — GCP post-auth sets the trigger
    // flag and dispatches this event AFTER isConnected is already true, so
    // the deps-based re-run won't catch it.
    const uid = userId;
    const onProviderChange = () => checkTrigger(uid);
    window.addEventListener("providerStateChanged", onProviderChange);

    return () => {
      stopPolling();
      if (syncedTimerRef.current) clearTimeout(syncedTimerRef.current);
      window.removeEventListener("providerStateChanged", onProviderChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supportsDiscovery, isConnected, userId]);

  if (!supportsDiscovery) return { syncStatus: "idle" };

  return { syncStatus };
}
