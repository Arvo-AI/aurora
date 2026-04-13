import { useCallback, useRef, useEffect, useState } from 'react';
import { Message } from '@/app/chat/types';
import { useChatHistory } from '@/hooks/useChatHistory';
import { SimpleChatUiState } from '@/hooks/useSessionPersistence';

interface UseSessionLoaderProps {
  onMessagesLoaded: (messages: Message[]) => void;
  onUiStateLoaded: (uiState: SimpleChatUiState) => void;
  onClearMessages: () => void;
}

export const useSessionLoader = ({
  onMessagesLoaded,
  onUiStateLoaded,
  onClearMessages,
}: UseSessionLoaderProps) => {
  const { loadSession, isLoadingSession } = useChatHistory();
  const lastIncidentIdRef = useRef<string | null>(null);
  const lastPollMessageCountRef = useRef<number>(0);
  const [pollingSessionId, setPollingSessionId] = useState<string | null>(null);

  // Poll for background session updates while in_progress
  useEffect(() => {
    if (!pollingSessionId) return;

    let cancelled = false;
    const interval = setInterval(async () => {
      if (cancelled) return;
      try {
        const res = await fetch(`/api/chat-sessions/${pollingSessionId}/status`);
        if (cancelled || !res.ok) return;
        const { status, message_count } = await res.json();

        if (status !== 'in_progress') {
          setPollingSessionId(null);
          // Fetch full messages one final time now that the session is done
          const sessionData = await loadSession(pollingSessionId);
          if (!cancelled && sessionData?.messages?.length) {
            onMessagesLoaded(sessionData.messages as Message[]);
          }
          return;
        }

        // Only do a full load when new messages exist
        if (message_count > lastPollMessageCountRef.current) {
          lastPollMessageCountRef.current = message_count;
          const sessionData = await loadSession(pollingSessionId);
          if (!cancelled && sessionData?.messages?.length) {
            onMessagesLoaded(sessionData.messages as Message[]);
          }
        }
      } catch {
        // Silently retry on next interval
      }
    }, 2000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [pollingSessionId, loadSession, onMessagesLoaded]);

  const loadSessionData = useCallback(async (sessionId: string): Promise<boolean> => {
    // Stop any existing polling for a previous session before loading a new one
    setPollingSessionId(prev => {
      if (prev && prev !== sessionId) {
        return null;
      }
      return prev;
    });

    try {
      const sessionData = await loadSession(sessionId);
      
      if (!sessionData) {
        console.warn(`Session ${sessionId} not found`);
        lastIncidentIdRef.current = null;
        return false;
      }

      lastIncidentIdRef.current = sessionData.incidentId || null;

      onClearMessages();

      if (sessionData.messages && sessionData.messages.length > 0) {
        onMessagesLoaded(sessionData.messages as Message[]);
      }

      if (sessionData.uiState) {
        const uiState = sessionData.uiState as any;
        const simplifiedUiState: SimpleChatUiState = {
          selectedModel: uiState.selectedModel,
          selectedMode: uiState.selectedMode, 
          selectedProviders: uiState.selectedProviders,
          input: uiState.input,
        };
        onUiStateLoaded(simplifiedUiState);
      }

      if (sessionData.status === 'in_progress') {
        lastPollMessageCountRef.current = sessionData.messages?.length || 0;
        setPollingSessionId(sessionId);
      }

      console.debug(`Loaded session ${sessionId} with ${sessionData.messages?.length || 0} messages`);
      return true;
    } catch (error) {
      console.error(`Failed to load session ${sessionId}:`, error);
      lastIncidentIdRef.current = null;
      return false;
    }
  }, [loadSession, onMessagesLoaded, onUiStateLoaded, onClearMessages]);

  return {
    loadSessionData,
    isLoadingSession,
    lastIncidentId: lastIncidentIdRef,
  };
};
