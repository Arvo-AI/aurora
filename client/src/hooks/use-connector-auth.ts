import { useState, useEffect, useCallback, useRef } from "react";

interface ConnectorStatus {
  connected: boolean;
}

interface UseConnectorAuthOptions<T extends ConnectorStatus> {
  cacheKey: string;
  storageKey: string;
  fetchStatus: () => Promise<T | null>;
  disconnectPath: string;
}

export function useConnectorAuth<T extends ConnectorStatus>({
  cacheKey,
  storageKey,
  fetchStatus,
  disconnectPath,
}: UseConnectorAuthOptions<T>) {
  const [status, setStatus] = useState<T | null>(null);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const fetchStatusRef = useRef(fetchStatus);
  fetchStatusRef.current = fetchStatus;

  const updateLocalState = useCallback(
    (result: T, { fireEvent = true } = {}) => {
      const prev = localStorage.getItem(cacheKey);
      const wasConnected = prev ? JSON.parse(prev)?.connected : false;

      setStatus(result);
      localStorage.setItem(cacheKey, JSON.stringify(result));
      if (result.connected) {
        localStorage.setItem(storageKey, "true");
      } else {
        localStorage.removeItem(storageKey);
      }
      if (fireEvent && wasConnected !== result.connected) {
        globalThis.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    },
    [cacheKey, storageKey],
  );

  const refresh = useCallback(async () => {
    try {
      const result = await fetchStatusRef.current();
      if (result !== null) updateLocalState(result);
    } catch {
      // leave current status as-is
    } finally {
      setIsCheckingStatus(false);
    }
  }, [updateLocalState]);

  useEffect(() => {
    const cached = localStorage.getItem(cacheKey);
    if (cached) {
      const parsed = JSON.parse(cached);
      setStatus(parsed);
      setIsCheckingStatus(false);
    }
    refresh();
  }, [cacheKey, refresh]);

  const disconnect = useCallback(async (): Promise<boolean> => {
    const response = await fetch(disconnectPath, {
      method: "DELETE",
      credentials: "include",
    });
    if (response.ok || response.status === 204) {
      setStatus({ connected: false } as T);
      localStorage.removeItem(cacheKey);
      localStorage.removeItem(storageKey);
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
      return true;
    }
    const text = await response.text();
    throw new Error(text || "Failed to disconnect");
  }, [cacheKey, storageKey, disconnectPath]);

  return {
    status,
    setStatus,
    isCheckingStatus,
    isConnected: Boolean(status?.connected),
    updateLocalState,
    disconnect,
    refresh,
  };
}
