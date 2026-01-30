import { useEffect, useState, useRef } from 'react';
import { VisualizationData } from '@/types/visualization';

interface VisualizationState {
  data: VisualizationData | null;
  isLoading: boolean;
  error: string | null;
  isConnected: boolean;
}

export function useVisualizationStream(incidentId: string) {
  const [state, setState] = useState<VisualizationState>({
    data: null,
    isLoading: true,
    error: null,
    isConnected: false,
  });
  
  const eventSourceRef = useRef<EventSource | null>(null);
  const currentVersionRef = useRef<number>(0);
  const fetchingUpdateRef = useRef<boolean>(false);

  useEffect(() => {
    let mounted = true;

    const fetchInitialData = async () => {
      try {
        const response = await fetch(`/api/incidents/${incidentId}/visualization`);
        
        if (!response.ok) {
          if (response.status === 404) {
            setState(prev => ({ ...prev, isLoading: false, data: null }));
            return;
          }
          throw new Error(`Failed to fetch: ${response.statusText}`);
        }

        const result = await response.json();
        if (mounted && result.data) {
          currentVersionRef.current = result.data.version || 0;
          setState({
            data: result.data,
            isLoading: false,
            error: null,
            isConnected: false,
          });
        }
      } catch (err) {
        if (mounted) {
          setState(prev => ({
            ...prev,
            isLoading: false,
            error: err instanceof Error ? err.message : 'Failed to load',
          }));
        }
      }
    };

    const connectSSE = () => {
      const eventSource = new EventSource(`/api/incidents/${incidentId}/visualization/stream`);
      eventSourceRef.current = eventSource;

      eventSource.onopen = () => {
        if (mounted) {
          setState(prev => ({ ...prev, isConnected: true }));
        }
      };

      eventSource.onmessage = async (event) => {
        try {
          const message = JSON.parse(event.data);
          
          if (message.type === 'connected') {
            return;
          }
          
          if (message.type === 'update' && message.version > currentVersionRef.current && !fetchingUpdateRef.current) {
            fetchingUpdateRef.current = true;
            try {
              const response = await fetch(`/api/incidents/${incidentId}/visualization`);
              if (response.ok) {
                const result = await response.json();
                if (mounted && result.data && result.data.version > currentVersionRef.current) {
                  currentVersionRef.current = result.data.version;
                  setState(prev => ({ ...prev, data: result.data }));
                }
              }
            } catch (err) {
              console.error('[Visualization] Failed to fetch update:', err);
            } finally {
              fetchingUpdateRef.current = false;
            }
          }
        } catch (err) {
          console.error('[Visualization] Failed to parse SSE message:', err);
        }
      };

      eventSource.onerror = () => {
        if (mounted) {
          setState(prev => ({ ...prev, isConnected: false }));
        }
      };
    };

    fetchInitialData().then(() => {
      connectSSE();
    });

    return () => {
      mounted = false;
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
  }, [incidentId]);

  return state;
}
