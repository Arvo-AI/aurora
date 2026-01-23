import { useCallback, useEffect, useRef, useState } from 'react';
import { useUser } from '@/hooks/useAuthHooks';

// Types for WebSocket messages
export interface WebSocketMessage {
  type: 'message' | 'code' | 'status' | 'deployment_step' | 'tool_call' | 'tool_result' | 'tool_error' | 'tool_status' | 'init' | 'usage_info' | 'stop_all_tools' | 'context_compressed' | 'error' | 'control' | 'toast_notification' | 'complete' | 'finished' | 'execution_confirmation';
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

    // Send initialization message with user_id
    const userId = user?.id;
    if (userId && wsRef.current) {
      const initMessage: WebSocketMessage = {
        type: 'init',
        user_id: userId
      };
      wsRef.current.send(JSON.stringify(initMessage));
    } else {
      console.warn('Cannot send init message - user not loaded or websocket not ready:', {
        hasUser: !!user?.id,
        hasConfigUserId: !!configRef.current.userId,
        hasWebSocket: !!wsRef.current,
        userId: user?.id,
        configUserId: configRef.current.userId
      });
    }

    configRef.current.onConnect?.();
  }, [user?.id]);

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

  const handleClose = useCallback(() => {
    if (!mountedRef.current) return;

    setState(prev => ({
      ...prev,
      isConnected: false,
      isConnecting: false
    }));

    configRef.current.onDisconnect?.();

    const maxAttempts = configRef.current.maxReconnectAttempts || 10;

    if (shouldReconnectRef.current && 
        state.reconnectAttempts < maxAttempts) {
      
      setState(prev => ({
        ...prev,
        reconnectAttempts: prev.reconnectAttempts + 1
      }));

      // Exponential backoff with jitter
      const baseDelay = Math.min(1000 * Math.pow(2, state.reconnectAttempts), 30000);
      const jitter = Math.random() * 1000; // Add up to 1 second of jitter
      const reconnectDelay = baseDelay + jitter;
      
      
      reconnectTimeoutRef.current = setTimeout(() => {
        if (mountedRef.current && shouldReconnectRef.current) {
          // Call connect through a ref to avoid circular dependency
          connectRef.current();
        }
      }, reconnectDelay);
    } else if (state.reconnectAttempts >= maxAttempts) {
      console.error(`WebSocket reconnection failed after ${maxAttempts} attempts`);
      setState(prev => ({
        ...prev,
        error: `Connection failed after ${maxAttempts} attempts`
      }));
    }
  }, [state.reconnectAttempts]);

  // Create a ref for connect function to avoid circular dependency
  const connectRef = useRef<() => void>(() => {});

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
      const ws = new WebSocket(configRef.current.url);
      
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

  // Auto-connect when user is available
  useEffect(() => {
    const hasUserId = user?.id;
    
    if (hasUserId && !state.isConnected && !state.isConnecting) {
      shouldReconnectRef.current = true;
      // Add a small delay to prevent race conditions in React StrictMode
      const timer = setTimeout(() => {
        if (mountedRef.current && !state.isConnected && !state.isConnecting) {
          connect();
        }
      }, 100);
      
      return () => clearTimeout(timer);
    }
  }, [user?.id, configRef.current.userId, state.isConnected, state.isConnecting, connect]);

  return {
    ...state,
    connect,
    disconnect,
    send,
    sendRaw,
    isReady: state.isConnected && (user?.id || configRef.current.userId),
    wsRef // Expose wsRef for better state checking
  };
}; 