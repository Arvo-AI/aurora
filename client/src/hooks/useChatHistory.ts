import { useState, useEffect, useCallback, useRef } from 'react';
import { useUser } from '@/hooks/useAuthHooks';
import { CompleteUiState, getDefaultUiState } from '@/utils/sessionStateUtils';
import { cleanupStaleToolCalls } from '@/utils/toolStateCleanup';
import { useQuery, queryClient, type Fetcher } from '@/lib/query';

export interface ChatSession {
  id: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
  ui_state?: CompleteUiState;
  status?: 'active' | 'in_progress' | 'completed' | 'failed';
}

// Legacy interface for backward compatibility
export interface UiState {
  isSidePanelVisible?: boolean;
  activeTab?: string;
  isCodeSectionExpanded?: boolean;
  files?: Array<{
    name: string;
    content: string;
    type: string;
    relativePath: string;
  }>;
  fileContents?: Record<string, string>;
  currentFilePath?: string;
  editorCode?: string;
  terraformCode?: string;
  envVars?: Array<{key: string; value: string}>;
  envFilePath?: string;
  envFileContent?: string;
}

export interface ChatMessage {
  id: number;
  text: string;
  sender: 'user' | 'bot';
  severity?: string;
  isThinking?: boolean;
  isStreaming?: boolean;
  isDeploymentStatus?: boolean;
  isCompleted?: boolean;
  toolCalls?: Array<{
    id: string;
    tool_name: string;
    input: string;
    output?: any;  // Changed from string | null to any to handle objects from backend
    error?: string | null;
    status: 'running' | 'completed' | 'error' | 'cancelled' | 'awaiting_confirmation';
    timestamp: string;
  }>;
  images?: Array<{
    data: string;
    displayData?: string;
    name?: string;
    type?: string;
  }>;
}

export interface UseChatHistoryReturn {
  sessions: ChatSession[];
  loading: boolean;
  isLoadingSession: boolean;
  error: string | null;
  currentSessionId: string | null;
  setCurrentSessionId: (sessionId: string | null) => void;
  lastRefreshTimestamp: number;
  refreshSessions: () => Promise<boolean>;
  createSession: (title?: string) => Promise<string | null>;
  loadSession: (sessionId: string) => Promise<{ messages: ChatMessage[], uiState?: CompleteUiState, incidentId?: string | null, status?: string } | null>;
  updateSession: (sessionId: string, title?: string, messages?: ChatMessage[], uiState?: CompleteUiState) => Promise<boolean>;
  deleteSession: (sessionId: string) => Promise<boolean>;
  switchToSession: (sessionId: string, onClearState: () => void, onApplyState: (uiState: CompleteUiState) => void) => Promise<{ messages: ChatMessage[], uiState?: CompleteUiState } | null>;
      createNewSession: (onClearState: () => void, initialTitle?: string) => Promise<string | null>;
    deleteAllSessions: (currentChatId: string | null) => Promise<boolean>;
    cleanupPhantomSessions: () => Promise<void>;
  }

export function useChatHistory(): UseChatHistoryReturn {
  const { user } = useUser();
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [lastRefreshTimestamp, setLastRefreshTimestamp] = useState<number>(0);

  const sessionsFetcher: Fetcher<ChatSession[]> = useCallback(async (_key, signal) => {
    const res = await fetch('/api/chat-sessions', {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      signal,
    });
    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(errorData.error || 'Failed to fetch chat sessions');
    }
    const data = await res.json();
    return (data.sessions || []) as ChatSession[];
  }, []);

  const {
    data: sessions,
    error: sessionsError,
    isLoading: loading,
    mutate: refetchSessions,
  } = useQuery<ChatSession[]>(
    user?.id ? '/api/chat-sessions' : null,
    sessionsFetcher,
    {
      staleTime: 10_000,
      retryCount: 3,
      retryDelay: 2000,
      revalidateOnFocus: true,
      onSuccess: () => setLastRefreshTimestamp(Date.now()),
    },
  );

  const sessionsList = sessions ?? [];
  const [crudError, setCrudError] = useState<string | null>(null);
  const error = crudError ?? sessionsError?.message ?? null;

  const refreshSessions = useCallback(async (_silent?: boolean): Promise<boolean> => {
    try {
      await refetchSessions();
      return true;
    } catch {
      return false;
    }
  }, [refetchSessions]);

  const createSession = useCallback(async (title?: string): Promise<string | null> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return null;
    
    try {
      setCrudError(null);
      
      const response = await fetch('/api/chat-sessions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title: title || 'New Chat',
          messages: [],
          uiState: getDefaultUiState(),
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to create chat session');
      }

      const data = await response.json();
      
      // Optimistically add the new session to the UI to avoid race conditions
      const newSession: ChatSession = {
        id: data.id,
        title: title || 'New Chat',
        message_count: 0, // New session starts with 0 messages
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        ui_state: getDefaultUiState(),
      };
      queryClient.set<ChatSession[]>('/api/chat-sessions', [newSession, ...sessionsList]);
      
      // A full refresh after the optimistic update is often redundant if the UI is driven
      // by the optimistic state. We can remove it for performance, but it's safer to leave
      // it to ensure eventual consistency. Let's keep it for now.
      await refreshSessions(); 
      return data.id;
    } catch (err) {
      console.error('Error creating chat session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to create chat session');
      return null;
    }
  }, [user?.id, refreshSessions]);

  const loadSession = useCallback(async (sessionId: string): Promise<{ messages: ChatMessage[], uiState?: CompleteUiState, incidentId?: string | null, status?: string } | null> => {
    const effectiveUserId = user?.id;
    
    
    if (!effectiveUserId) {
      console.warn('️ [useChatHistory] No effective user ID for loadSession');
      return null;
    }
    
    try {
      setCrudError(null);
      setIsLoadingSession(true);
      const fetchStart = performance.now();
      const response = await fetch(`/api/chat-sessions/${sessionId}`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
        cache: 'no-store',  // Prevent caching - always fetch fresh data on reload
      });

      if (response.status === 404) {
        // Session not found - this is expected for deleted sessions
        return null;
      }

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('Failed to load chat session:', errorData);
        throw new Error(errorData.error || 'Failed to load chat session');
      }

      const data = await response.json();
      const fetchTime = performance.now() - fetchStart;
      
      setCurrentSessionId(sessionId);
      
      // Ensure we always return a complete UI state
      const uiState = data.ui_state || getDefaultUiState();
      
      // Clean up any stale tool calls (tools stuck in "running" state from crashed workflows)
      const cleanupStart = performance.now();
      const rawMessages = data.messages || [];
      const cleanedMessages = cleanupStaleToolCalls(rawMessages, data.updated_at);
      const cleanupTime = performance.now() - cleanupStart;
      // DON'T refresh sessions when just loading - this prevents sessions from moving to top
      return { messages: cleanedMessages, uiState, incidentId: data.incident_id || null, status: data.status || null };
    } catch (err) {
      console.error('[useChatHistory] Error loading chat session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to load chat session');
      return null;
    } finally {
      setIsLoadingSession(false);
      
    }
  }, [user?.id]);

  const updateSession = useCallback(async (sessionId: string, title?: string, messages?: ChatMessage[], uiState?: CompleteUiState): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setCrudError(null);
      
      const response = await fetch(`/api/chat-sessions/${sessionId}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title,
          messages,
          uiState,
        }),
      });

      if (response.status === 404) {
        // Session not found - this might happen if it was deleted elsewhere
        console.debug(`Chat session ${sessionId} not found during update`);
        return false;
      }

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to update chat session');
      }

      // Don't refresh on every update to avoid refresh loops
      // The optimistic updates in the UI handle most cases
      return true;
    } catch (err) {
      console.error('Error updating chat session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to update chat session');
      return false;
    }
  }, [user?.id, refreshSessions]);

  const deleteSession = useCallback(async (sessionId: string): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setCrudError(null);
      
      const response = await fetch(`/api/chat-sessions/${sessionId}`, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (response.status === 404) {
        // Session not found - it might have been deleted already
        console.debug(`Chat session ${sessionId} not found during deletion`);
        // Still consider this a success since the end goal (session deleted) is achieved
        if (currentSessionId === sessionId) {
          setCurrentSessionId(null);
        }
        await refreshSessions(); // Refresh to update the list
        return true;
      }

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to delete chat session');
      }

      // If we deleted the current session, clear it
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null);
      }

      await refreshSessions(); // Refresh when deleting session
      return true;
    } catch (err) {
      console.error('Error deleting chat session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to delete chat session');
      return false;
    }
  }, [user?.id, currentSessionId, refreshSessions]);

  const deleteAllSessions = useCallback(async (currentChatId: string | null): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setCrudError(null);
      
      // Always build URL with current_session_id parameter
      let url = `/api/chat-sessions/bulk-delete`;
      if (currentChatId) {
        url += `?current_session_id=${encodeURIComponent(currentChatId)}`;
      }
      console.log(`Bulk delete URL: ${url}`);
      
      const response = await fetch(url, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to delete all chat sessions');
      }

      // Only clear current session ID if no current chat was provided
      if (!currentChatId) {
        setCurrentSessionId(null);
      }
      
      await refreshSessions(); // Refresh after deleting sessions
      return true;
    } catch (err) {
      console.error('Error deleting all chat sessions:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to delete all chat sessions');
      return false;
    }
  }, [user?.id, refreshSessions]);


  // New function for session switching with state management
  const switchToSession = useCallback(async (
    sessionId: string, 
    onClearState: () => void, 
    onApplyState: (uiState: CompleteUiState, sessionData?: { messages: ChatMessage[], uiState?: CompleteUiState }) => void
  ): Promise<{ messages: ChatMessage[], uiState?: CompleteUiState } | null> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return null;
    
    try {
      setIsLoadingSession(true);
      
      // Step 1: Load new session data FIRST (before clearing anything)
      const sessionData = await loadSession(sessionId);
      
      if (sessionData) {
        // Step 2: Clear current state and apply new state atomically
        onClearState();
        
        // Step 3: Immediately apply new state AND messages to prevent visual flash
        const uiState = sessionData.uiState || getDefaultUiState();
        onApplyState(uiState, sessionData);
        
        return sessionData;
      }
      
      return null;
    } catch (err) {
      console.error('Error switching to session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to switch session');
      return null;
    } finally {
      setIsLoadingSession(false);
    }
  }, [user?.id, loadSession]);

  // New function for creating a new session with clean state
  const createNewSession = useCallback(async (onClearState: () => void, initialTitle?: string): Promise<string | null> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return null;
    
    try {
      // Create new session FIRST to get the ID
      const newSessionId = await createSession(initialTitle);
      
      if (newSessionId) {
        // CRITICAL: Set the new session ID BEFORE clearing state to prevent race conditions
        setCurrentSessionId(newSessionId);
        
        // Clear current state for fresh start
        onClearState();
        
        return newSessionId;
      }
      
      return null;
    } catch (err) {
      console.error('Error creating new session:', err);
      setCrudError(err instanceof Error ? err.message : 'Failed to create new session');
      return null;
    }
  }, [user?.id, createSession]);

  // refreshSessionsRef used by polling and visibility handlers
  const refreshSessionsRef = useRef(refreshSessions);
  refreshSessionsRef.current = refreshSessions;

  // Cleanup function to remove phantom sessions (sessions that exist in backend but not in current frontend state)
  const cleanupPhantomSessions = useCallback(async (): Promise<void> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return;
    
    try {
      const cached = queryClient.read<ChatSession[]>('/api/chat-sessions');
      if (!cached || cached.length === 0) return;

      const FIVE_MINUTES_MS = 5 * 60 * 1000;
      const now = Date.now();

      const phantomSessions = cached.filter((session) => {
        const createdAt = session.created_at ? Date.parse(session.created_at) : 0;
        const isOld = now - createdAt > FIVE_MINUTES_MS;

        return (
          session.message_count === 0 &&
          (session.title === 'New Chat' || !session.title) &&
          isOld
        );
      });
      
      if (phantomSessions.length > 0) {
        for (const phantom of phantomSessions) {
          try {
            await fetch(`/api/chat-sessions/${phantom.id}`, { method: 'DELETE' });
          } catch {
            // best-effort cleanup
          }
        }
        await refreshSessions();
      }
    } catch {
      // non-critical, swallow
    }
  }, [user?.id, refreshSessions]);

  // Polling for in_progress sessions (silent - no loading indicator)
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const hasInProgress = sessionsList.some(s => s.status === 'in_progress');

  useEffect(() => {
    // Clear any existing interval
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }

    // Only poll if there are in_progress sessions
    if (!hasInProgress) {
      return;
    }

    // Poll every 3 seconds (silent - no loading indicator)
    pollIntervalRef.current = setInterval(() => {
      refreshSessionsRef.current(true); // silent=true - no loading skeleton
    }, 3000);

    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
    };
  }, [hasInProgress]);

  return {
    sessions: sessionsList,
    loading,
    error,
    currentSessionId,
    setCurrentSessionId,
    lastRefreshTimestamp,
    refreshSessions,
    createSession,
    loadSession,
    updateSession,
    deleteSession,
    switchToSession,
    createNewSession,
    isLoadingSession,
    deleteAllSessions,
    cleanupPhantomSessions,
  };
} 