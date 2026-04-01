import { useEffect, useState, useRef, useCallback } from 'react';

export interface SystemHealthData {
  timestamp: string;
  services: Record<string, { status: string; message?: string; error?: string; warning?: string }>;
  celery: {
    worker_count: number;
    workers: string[];
    active_tasks: number;
    reserved_tasks: number;
    scheduled_tasks: number;
    queue_depth: number | null;
  };
}

interface SystemHealthState {
  health: SystemHealthData | null;
  isConnected: boolean;
  error: string | null;
  eventLog: SystemHealthData[];
}

export function useSystemHealthStream() {
  const [state, setState] = useState<SystemHealthState>({
    health: null,
    isConnected: false,
    error: null,
    eventLog: [],
  });

  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const es = new EventSource('/api/monitor/health/stream');
    eventSourceRef.current = es;

    es.onopen = () => {
      setState(prev => ({ ...prev, isConnected: true, error: null }));
    };

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.error) return;
        const healthData = data as SystemHealthData;
        setState(prev => ({
          ...prev,
          health: healthData,
          eventLog: [...prev.eventLog.slice(-49), healthData],
        }));
      } catch {
        // ignore malformed events
      }
    };

    es.onerror = () => {
      setState(prev => ({ ...prev, isConnected: false, error: 'Connection lost' }));
      es.close();
      reconnectTimerRef.current = setTimeout(connect, 5000);
    };
  }, []);

  useEffect(() => {
    connect();

    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    };
  }, [connect]);

  return state;
}
