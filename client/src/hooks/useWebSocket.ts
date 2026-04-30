import { useCallback, useEffect, useRef, useState } from 'react';
import { useUser } from '@/hooks/useAuthHooks';

// Types for WebSocket messages
export interface WebSocketMessage {
  type: 'message' | 'code' | 'status' | 'deployment_step' | 'tool_call' | 'tool_result' | 'tool_error' | 'tool_status' | 'init' | 'usage_info' | 'usage_update' | 'usage_final' | 'stop_all_tools' | 'context_compressed' | 'error' | 'control' | 'toast_notification' | 'complete' | 'finished' | 'execution_confirmation';
  data?: any;
  step_id?: string;
  status?: string;
  message?: string;
  query?: string; // Chat query message field
  task_id?: string;
  tool_name?: string;
  input?: any;
  output?: any;
  error?: string;
  timestamp?: string;
  user_id?: string;
  session_id?: string; // Session ID for message correlation
  action?: string; // For control messages
  isComplete?: boolean; // Flag to indicate workflow completion
  // Additional fields expected by backend
  provider_preference?: string | string[]; // Cloud provider preference
  selected_project_id?: string; // Selected GCP project ID
  model?: string; // Selected AI model
  mode?: string; // Chat mode (agent/ask)
  attachments?: any[]; // File attachments
  direct_tool_call?: any; // Direct tool call data
  ui_state?: {
    selectedModel?: string;
    selectedMode?: string;
    selectedProviders?: string[];
  }; // UI state to save with the session
}

export interface WebSocketConfig {
  url: string;
  userId?: string | null;
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
  onMessage?: (message: WebSocketMessage) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onError?: (error: Event) => void;
}

export interface WebSocketState {
  isConnected: boolean;
  isConnecting: boolean;
  error: string | null;
  reconnectAttempts: number;
}

export const useWebSocket = (config: WebSocketConfig) => {
  const { user } = useUser();
  const [state, setState] = useState<WebSocketState>({
    isConnected: false,
    isConnecting: false,
    error: null,
    reconnectAttempts: 0
  });
  
  // Track userId changes to trigger reconnection
  const [trackedUserId, setTrackedUserId] = useState<string | null>(config.userId || null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const shouldReconnectRef = useRef(true);
  const mountedRef = useRef(true);

  // Store config in ref to avoid stale closures
  const configRef = useRef(config);
  configRef.current = config;

  const cleanup = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.removeEventListener('open', handleOpen);
      wsRef.current.removeEventListener('message', handleMessage);
      wsRef.current.removeEventListener('error', handleError);
      wsRef.current.removeEventListener('close', handleClose);
      
      if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
        wsRef.current.close();
      }
      wsRef.current = null;
    }
  }, []);

  const handleOpen = useCallback(() => {
    if (!mountedRef.current) return;

    setState(prev => ({
      ...prev,
      isConnected: true,
      isConnecting: false,
      error: null,
      reconnectAttempts: 0
    }));

    configRef.current.onConnect?.();
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    if (!mountedRef.current) return;

    try {
      const message: WebSocketMessage = JSON.parse(event.data);
      configRef.current.onMessage?.(message);
    } catch (error) {
      console.error('Failed to parse WebSocket message:', error);
      // console.error('Raw message:', event.data);
      
      // Handle cloud tool outputs that might contain nested JSON gracefully
      if (event.data && typeof event.data === 'string') {
        if (event.data.includes('gleapis.co') || event.data.includes('gcloud') || event.data.includes('"success"')) {
          console.warn('Received cloud tool output with embedded JSON, handling gracefully');
          return;
        }
      }
    }
  }, []);

  const handleError = useCallback((error: Event) => {
    if (!mountedRef.current) return;

    console.error('WebSocket error:', error);
    setState(prev => ({
      ...prev,
      error: 'WebSocket connection error',
      isConnecting: false
    }));

    configRef.current.onError?.(error);

    // Force-close the socket so that handleClose logic will attempt reconnection.
    // Some browsers don't automatically emit "close" after a protocol error.
    if (wsRef.current && wsRef.current.readyState !== WebSocket.CLOSED) {
      try {
        wsRef.current.close();
      } catch (_) {
        /* ignore */
      }
    }
  }, []);

  // Create a ref for connect function to avoid circular dependency
  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    const maxAttempts = configRef.current.maxReconnectAttempts || 3;
    if (!shouldReconnectRef.current || state.reconnectAttempts >= maxAttempts) {
      if (state.reconnectAttempts >= maxAttempts) {
        setState(prev => ({
          ...prev,
          error: `Connection failed after ${maxAttempts} attempts`
        }));
      }
      return;
    }
    setState(prev => ({
      ...prev,
      reconnectAttempts: prev.reconnectAttempts + 1
    }));
    const baseDelay = Math.min(1000 * Math.pow(2, state.reconnectAttempts), 30000);
    const jitter = crypto.getRandomValues(new Uint32Array(1))[0] / (0xFFFFFFFF / 1000);
    reconnectTimeoutRef.current = setTimeout(() => {
      if (mountedRef.current && shouldReconnectRef.current) {
        connectRef.current();
      }
    }, baseDelay + jitter);
  }, [state.reconnectAttempts]);

  const handleClose = useCallback(() => {
    if (!mountedRef.current) return;

    setState(prev => ({
      ...prev,
      isConnected: false,
      isConnecting: false
    }));

    configRef.current.onDisconnect?.();

    scheduleReconnect();
  }, [scheduleReconnect]);

  const connect = useCallback(async () => {
    if (!mountedRef.current) return;
    
    
    // Prevent multiple simultaneous connections
    if (wsRef.current && (wsRef.current.readyState === WebSocket.CONNECTING || 
        wsRef.current.readyState === WebSocket.OPEN)) {
      console.log('WebSocket already connecting or connected, skipping new connection');
      return;
    }

    // Clean up existing connection
    cleanup();

    setState(prev => ({
      ...prev,
      isConnecting: true,
      error: null
    }));

    try {
      let wsUrl = configRef.current.url;

      // Fetch a fresh token per connection (tokens are single-use via jti enforcement)
      let token: string | null = null;
      try {
        const tokenRes = await fetch('/api/ws-token');
        if (tokenRes.ok) {
          const data = await tokenRes.json();
          token = data.token || null;
        } else if (tokenRes.status === 401 || tokenRes.status === 403) {
          console.error('WS token auth failed (session expired), stopping reconnect');
          shouldReconnectRef.current = false;
          setState(prev => ({
            ...prev,
            isConnecting: false,
            error: 'Session expired — please log in again'
          }));
          return;
        } else {
          console.error(`WS token fetch failed (status ${tokenRes.status}), will retry on next reconnect`);
          setState(prev => ({
            ...prev,
            isConnecting: false,
            error: 'Unable to obtain WebSocket token, retrying...'
          }));
          scheduleReconnect();
          return;
        }
      } catch (tokenErr) {
        console.error('Network error fetching WS token, will retry on next reconnect:', tokenErr);
        setState(prev => ({
          ...prev,
          isConnecting: false,
          error: 'Network error obtaining WebSocket token, retrying...'
        }));
        scheduleReconnect();
        return;
      }

      // Build URL with token, replacing any existing token param
      if (token) {
        const parsed = new URL(wsUrl, globalThis.location.origin);
        parsed.searchParams.set('token', token);
        wsUrl = parsed.toString();
      }

      const ws = new WebSocket(wsUrl);
      
      // Set binary type to handle different frame types
      ws.binaryType = 'arraybuffer';
      
      wsRef.current = ws;

      ws.addEventListener('open', handleOpen);
      ws.addEventListener('message', handleMessage);
      ws.addEventListener('error', handleError);
      ws.addEventListener('close', handleClose);

    } catch (error) {
      console.error('Failed to create WebSocket connection:', error);
      setState(prev => ({
        ...prev,
        isConnecting: false,
        error: 'Failed to create WebSocket connection'
      }));
    }
  }, [cleanup, handleOpen, handleMessage, handleError, handleClose]);

  // Update connect ref whenever connect function changes
  connectRef.current = connect;

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    cleanup();
    setState(prev => ({
      ...prev,
      isConnected: false,
      isConnecting: false,
      error: null,
      reconnectAttempts: 0
    }));
  }, [cleanup]);

  const send = useCallback((message: WebSocketMessage) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  const sendRaw = useCallback((data: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(data);
      return true;
    }
    return false;
  }, []);

  // Cleanup function
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cleanup();
    };
  }, [cleanup]);

  // Watch for userId changes and update tracked value
  useEffect(() => {
    if (config.userId !== trackedUserId) {
      setTrackedUserId(config.userId || null);
      
      // If we have a new userId and no connection, trigger connect
      if (config.userId && !state.isConnected && !state.isConnecting) {
        shouldReconnectRef.current = true;
        const timer = setTimeout(() => {
          if (mountedRef.current && !state.isConnected && !state.isConnecting) {
            connect();
          }
        }, 100);
        
        return () => clearTimeout(timer);
      }
    }
  }, [config.userId, trackedUserId, state.isConnected, state.isConnecting, connect]);

  // Auto-connect when user is available. An empty config.url means the caller
  // disabled this transport (e.g. SSE chat mode wires an inert WebSocket to
  // keep the legacy hook tree intact); skip the token fetch + reconnect loop
  // entirely in that case so we don't pound /api/ws-token forever.
  useEffect(() => {
    const hasUserId = user?.id;
    const hasUrl = !!configRef.current.url;

    if (hasUserId && hasUrl && !state.isConnected && !state.isConnecting) {
      shouldReconnectRef.current = true;
      // Add a small delay to prevent race conditions in React StrictMode
      const timer = setTimeout(() => {
        if (mountedRef.current && !state.isConnected && !state.isConnecting) {
          connect();
        }
      }, 100);

      return () => clearTimeout(timer);
    }
  }, [user?.id, configRef.current.userId, configRef.current.url, state.isConnected, state.isConnecting, connect]);

  return {
    ...state,
    connect,
    disconnect,
    send,
    sendRaw,
    isReady: state.isConnected && !!(user?.id || configRef.current.userId),
    wsRef // Expose wsRef for better state checking
  };
};
