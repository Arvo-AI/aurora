import { useEffect, useRef, useCallback } from 'react';
import { useUser } from '@/hooks/useAuthHooks';
import { Message } from '@/app/chat/types';
import { useChatHistory } from '@/hooks/useChatHistory';
import { shouldDelayAutoSave, getToolCallSummary } from '@/utils/toolCallUtils';

// Simple UI state interface for the new chat page (much simpler than the deploy page)
export interface SimpleChatUiState {
  selectedModel?: string;
  selectedMode?: string;
  selectedProviders?: string[];
  input?: string;
}

interface UseSessionPersistenceProps {
  messages: Message[];
  currentSessionId: string | null;
  uiState: SimpleChatUiState;
  enabled?: boolean;
  hasCreatedSession?: boolean;
}

export const useSessionPersistence = ({
  messages,
  currentSessionId,
  uiState,
  enabled = true,
  hasCreatedSession = true,
}: UseSessionPersistenceProps) => {
  const { user } = useUser();
  const { updateSession } = useChatHistory();
  const lastSavedStateRef = useRef<string>('');
  const previousSessionIdRef = useRef<string | null>(null);

  const saveSession = useCallback(async () => {
    if (!currentSessionId || messages.length === 0 || !enabled || !hasCreatedSession || !user?.id) {
      if (!user?.id) {
        console.debug('[useSessionPersistence] Skipping save - no authenticated user');
      }
      return;
    }

    // Smart delay: Don't save while tool calls are active
    if (shouldDelayAutoSave(messages)) {
      const summary = getToolCallSummary(messages);
      //console.log('⏸️ Delaying auto-save - tool calls active:', summary);
      return;
    }

    try {
      // Create a state signature to avoid unnecessary saves
      const currentStateSignature = JSON.stringify({
        messagesCount: messages.length,
        lastMessageId: messages[messages.length - 1]?.id,
        uiState
      });
      
      // Only save if state has actually changed
      if (lastSavedStateRef.current === currentStateSignature) {
        return;
      }
      
      const success = await updateSession(
        currentSessionId,
        undefined, // Don't change title
        messages as any,
        uiState as any // Cast to CompleteUiState - it will handle the subset
      );
      
      if (success) {
        lastSavedStateRef.current = currentStateSignature;
      }
    } catch (error) {
      console.error('Failed to save session:', error);
    }
  }, [currentSessionId, messages, uiState, updateSession, enabled, hasCreatedSession, user?.id]);

  // Track session ID changes and save to the PREVIOUS session when switching
  useEffect(() => {
    const prevSessionId = previousSessionIdRef.current;
    previousSessionIdRef.current = currentSessionId;
    
    return () => {
      // Save to the PREVIOUS session ID when switching (not the current one!)
      if (prevSessionId && messages.length > 0 && enabled) {
        
        // Save to the PREVIOUS session ID (where these messages belong)
        updateSession(
          prevSessionId,
          undefined, // Don't change title
          messages as any,
          uiState as any
        ).catch(error => {
          console.error('Failed to save session during cleanup:', error);
        });
      }
    };
  }, [currentSessionId, messages, uiState, updateSession, enabled]);

  // Manual save function for explicit saves (e.g., triggered by specific UI events)
  const saveNow = useCallback(() => {
    return saveSession();
  }, [saveSession]);

  return {
    saveNow,
    isEnabled: enabled
  };
};
