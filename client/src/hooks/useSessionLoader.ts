import { useCallback } from 'react';
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

  const loadSessionData = useCallback(async (sessionId: string): Promise<boolean> => {
    try {
      const sessionData = await loadSession(sessionId);
      
      if (!sessionData) {
        console.warn(`Session ${sessionId} not found`);
        return false;
      }

      // Clear current messages first
      onClearMessages();

      // Load messages
      if (sessionData.messages && sessionData.messages.length > 0) {
        onMessagesLoaded(sessionData.messages as Message[]);
      }

      // Load UI state if available
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

      console.debug(`Loaded session ${sessionId} with ${sessionData.messages?.length || 0} messages`);
      return true;
    } catch (error) {
      console.error(`Failed to load session ${sessionId}:`, error);
      return false;
    }
  }, [loadSession, onMessagesLoaded, onUiStateLoaded, onClearMessages]);

  return {
    loadSessionData,
    isLoadingSession
  };
};
