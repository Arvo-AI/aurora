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
  const [pollingSessionId, setPollingSessionId] = useState<string | null>(null);

  // Poll for background session updates while in_progress
  useEffect(() => {
    if (!pollingSessionId) return;

    let cancelled = false;
    const interval = setInterval(async () => {
      if (cancelled) return;
      try {
        const sessionData = await loadSession(pollingSessionId);
        if (cancelled || !sessionData) return;

        if (sessionData.messages && sessionData.messages.length > 0) {
          onMessagesLoaded(sessionData.messages as Message[]);
        }

        if (sessionData.status !== 'in_progress') {
          setPollingSessionId(null);
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
