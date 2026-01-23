import { useState, useEffect, useCallback, useRef } from 'react';
import { useUser } from '@/hooks/useAuthHooks';
import { CompleteUiState, getDefaultUiState } from '@/utils/sessionStateUtils';
import { cleanupStaleToolCalls } from '@/utils/toolStateCleanup';

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
  refreshSessions: () => Promise<void>;
  createSession: (title?: string) => Promise<string | null>;
  loadSession: (sessionId: string) => Promise<{ messages: ChatMessage[], uiState?: CompleteUiState } | null>;
  updateSession: (sessionId: string, title?: string, messages?: ChatMessage[], uiState?: CompleteUiState) => Promise<boolean>;
  deleteSession: (sessionId: string) => Promise<boolean>;
  switchToSession: (sessionId: string, onClearState: () => void, onApplyState: (uiState: CompleteUiState) => void) => Promise<{ messages: ChatMessage[], uiState?: CompleteUiState } | null>;
      createNewSession: (onClearState: () => void, initialTitle?: string) => Promise<string | null>;
    deleteAllSessions: (currentChatId: string | null) => Promise<boolean>;
    cleanupPhantomSessions: () => Promise<void>;
  }

export function useChatHistory(): UseChatHistoryReturn {
  const { user } = useUser();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [lastRefreshTimestamp, setLastRefreshTimestamp] = useState<number>(0);
  
  const refreshSessions = useCallback(async (silent: boolean = false) => {
    const effectiveUserId = user?.id;
    
    
    if (!effectiveUserId) {
      console.warn('️ [useChatHistory] No effective user ID, skipping refresh');
      return;
    }
    
    try {
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      const response = await fetch('/api/chat-sessions', {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('Failed to fetch chat sessions:', errorData);
        throw new Error(errorData.error || 'Failed to fetch chat sessions');
      }

      const data = await response.json();
      const newSessions = data.sessions || [];
      
      // Only update state if sessions actually changed (prevents unnecessary re-renders)
      setSessions(prev => {
        // Quick length check
        if (prev.length !== newSessions.length) return newSessions;
        
        // Create a map of old sessions by ID for efficient lookup
        const prevMap = new Map(prev.map(s => [s.id, s]));
        
        // Compare by ID (not by index) since sessions are sorted by updated_at DESC
        // and order can shift when a session's updated_at changes
        const hasChanges = newSessions.some((newSession: ChatSession) => {
          const oldSession = prevMap.get(newSession.id);
          return !oldSession || 
                 oldSession.title !== newSession.title ||
                 oldSession.status !== newSession.status ||
                 oldSession.message_count !== newSession.message_count ||
                 oldSession.updated_at !== newSession.updated_at;
        });
        
        return hasChanges ? newSessions : prev;
      });
      
      if (!silent) {
        setLastRefreshTimestamp(Date.now());
      }
    } catch (err) {
      console.error('[useChatHistory] Error fetching chat sessions:', err);
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to fetch chat sessions');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [user?.id]);

  const createSession = useCallback(async (title?: string): Promise<string | null> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return null;
    
    try {
      setError(null);
      
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
      setSessions(prevSessions => [newSession, ...prevSessions]);
      
      // A full refresh after the optimistic update is often redundant if the UI is driven
      // by the optimistic state. We can remove it for performance, but it's safer to leave
      // it to ensure eventual consistency. Let's keep it for now.
      await refreshSessions(); 
      return data.id;
    } catch (err) {
      console.error('Error creating chat session:', err);
      setError(err instanceof Error ? err.message : 'Failed to create chat session');
      return null;
    }
  }, [user?.id, refreshSessions]);

  const loadSession = useCallback(async (sessionId: string): Promise<{ messages: ChatMessage[], uiState?: CompleteUiState } | null> => {
    const effectiveUserId = user?.id;
    
    
    if (!effectiveUserId) {
      console.warn('️ [useChatHistory] No effective user ID for loadSession');
      return null;
    }
    
    try {
      setError(null);
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
      return { messages: cleanedMessages, uiState };
    } catch (err) {
      console.error('[useChatHistory] Error loading chat session:', err);
      setError(err instanceof Error ? err.message : 'Failed to load chat session');
      return null;
    } finally {
      setIsLoadingSession(false);
      
    }
  }, [user?.id]);

  const updateSession = useCallback(async (sessionId: string, title?: string, messages?: ChatMessage[], uiState?: CompleteUiState): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setError(null);
      
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
      setError(err instanceof Error ? err.message : 'Failed to update chat session');
      return false;
    }
  }, [user?.id, refreshSessions]);

  const deleteSession = useCallback(async (sessionId: string): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setError(null);
      
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
      setError(err instanceof Error ? err.message : 'Failed to delete chat session');
      return false;
    }
  }, [user?.id, currentSessionId, refreshSessions]);

  const deleteAllSessions = useCallback(async (currentChatId: string | null): Promise<boolean> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return false;
    
    try {
      setError(null);
      
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
      setError(err instanceof Error ? err.message : 'Failed to delete all chat sessions');
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
      setError(err instanceof Error ? err.message : 'Failed to switch session');
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
      setError(err instanceof Error ? err.message : 'Failed to create new session');
      return null;
    }
  }, [user?.id, createSession]);

  // Auto-refresh sessions when user ID changes (NOT when refreshSessions changes)
  const refreshSessionsRef = useRef(refreshSessions);
  refreshSessionsRef.current = refreshSessions;
  
  // Track if initial load has been done
  const hasLoadedRef = useRef(false);
  const previousUserIdRef = useRef<string | null | undefined>(null);
  
  useEffect(() => {
    const effectiveUserId = user?.id;
    
    // Reset hasLoadedRef if the user ID changes
    if (previousUserIdRef.current !== null && previousUserIdRef.current !== effectiveUserId) {
      hasLoadedRef.current = false;
    }
    previousUserIdRef.current = effectiveUserId;
    
    if (effectiveUserId && !hasLoadedRef.current) {
      hasLoadedRef.current = true;
      refreshSessionsRef.current(); // Show loading on initial load only
    }
  }, [user?.id]); // Only re-run when user ID changes

  // Cleanup function to remove phantom sessions (sessions that exist in backend but not in current frontend state)
  const cleanupPhantomSessions = useCallback(async (): Promise<void> => {
    const effectiveUserId = user?.id;
    if (!effectiveUserId) return;
    
    try {
      console.debug('Starting phantom session cleanup...');
      
      // Get all sessions from backend
      const response = await fetch('/api/chat-sessions', {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        console.debug('Failed to fetch sessions for cleanup');
        return;
      }

      const data = await response.json();
      const backendSessions = data.sessions || [];
      
      // Find sessions that exist in backend but have no messages (phantom sessions)
      // Only target sessions that are at least 5 minutes old to avoid deleting
      // chats that the user has just created but has not typed in yet.
      const FIVE_MINUTES_MS = 5 * 60 * 1000;
      const now = Date.now();

      const phantomSessions = backendSessions.filter((session: any) => {
        const createdAt = session.created_at ? Date.parse(session.created_at) : 0;
        const isOld = now - createdAt > FIVE_MINUTES_MS;

        return (
          session.message_count === 0 &&
          (session.title === 'New Chat' || !session.title) &&
          isOld
        );
      });
      
      if (phantomSessions.length > 0) {
        console.debug(`Found ${phantomSessions.length} phantom sessions, cleaning up...`);
        
        // Delete phantom sessions
        for (const phantom of phantomSessions) {
          try {
            const deleteResponse = await fetch(`/api/chat-sessions/${phantom.id}`, {
              method: 'DELETE',
              headers: {
                'Content-Type': 'application/json',
              },
            });
            
            if (deleteResponse.ok) {
              console.debug(`Cleaned up phantom session: ${phantom.id}`);
            } else {
              console.debug(`Failed to cleanup phantom session: ${phantom.id}`);
            }
          } catch (err) {
            console.debug(`Error cleaning up phantom session ${phantom.id}:`, err);
          }
        }
        
        // Refresh sessions to update UI
        await refreshSessions();
      } else {
        console.debug('No phantom sessions found');
      }
    } catch (err) {
      console.debug('Error during phantom session cleanup:', err);
    }
  }, [user?.id, refreshSessions]);

  // Polling for in_progress sessions (silent - no loading indicator)
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const hasInProgress = sessions.some(s => s.status === 'in_progress');

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

  // Refresh on window focus to catch new background chats (silent)
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        refreshSessionsRef.current(true); // silent=true
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  return {
    sessions,
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
    cleanupPhantomSessions, // Export cleanup function
  };
} 