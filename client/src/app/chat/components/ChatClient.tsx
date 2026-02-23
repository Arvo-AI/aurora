"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useUser } from '@/hooks/useAuthHooks';
import { getEnv } from '@/lib/env';
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import dynamic from "next/dynamic";
// Core chat components
import { VirtualizedMessages } from "@/components/chat/virtualized-messages";
import EnhancedChatInput from "@/components/chat/enhanced-chat-input";
import EmptyStateHeader from "@/components/chat/empty-state-header";

// Dynamic imports for heavy components
const DynamicPrompts = dynamic(() => import("@/components/DynamicPrompts"), {
  ssr: false,
  loading: () => <div className="h-20 bg-muted animate-pulse rounded mt-6 w-full max-w-3xl" />
});

// Hooks and utilities
import { useWebSocket } from "@/hooks/useWebSocket";
import { useChatHistory } from "@/hooks/useChatHistory";
import { Message } from "../types";
import { useStreamingMessages } from '@/hooks/useStreamingMessages';
import { useMessageHandler } from '@/hooks/useMessageHandler';
import { SimpleChatUiState } from '@/hooks/useSessionPersistence';
import { useSessionLoader } from '@/hooks/useSessionLoader';
import { useChatExpansion } from '@/app/components/ClientShell';
import { useChatCancellation } from '@/hooks/useChatCancellation';
import { useChatSendHandlers } from "./useChatSendHandlers";

interface ChatClientProps {
  initialSessionId?: string;
  shouldStartNewChat?: boolean;
  initialMessage?: string;
  incidentContext?: string;
  initialMode?: string;
}

export default function ChatClient({ initialSessionId, shouldStartNewChat, initialMessage, incidentContext, initialMode }: ChatClientProps) {
  const { user, isLoaded } = useUser();
  const router = useRouter();
  
  // Core state
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [images, setImages] = useState<Array<{file: File, preview: string}>>([]);
  const [userId, setUserId] = useState<string | null>(null);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [isLoadingSessionMessages, setIsLoadingSessionMessages] = useState(false);
  const [hasCreatedSession, setHasCreatedSession] = useState(false);
  const justCreatedSessionRef = useRef<string | null>(null);
  const lastLoadedSessionRef = useRef<string | null>(null);
  const initialMessageSentRef = useRef<boolean>(false);
  const [activeIncidentContext, setActiveIncidentContext] = useState<string | undefined>(incidentContext);
  
  
  // Modular streaming message handling
  const streamingMessages = useStreamingMessages();
  const { checkIsStreaming, finishStreamingMessage, cleanup: cleanupStreaming } = streamingMessages;

  // Chat history integration
  const { createSession, loadSession } = useChatHistory();

  // Layout context integration for chat history navigation
  const { 
    setOnChatSessionSelect, 
    setOnNewChat, 
    setCurrentChatSessionId,
    refreshChatHistory
  } = useChatExpansion();

  // Message handling callbacks
  const onNewMessage = useCallback((message: Message) => {
    setMessages(prev => [...prev, message]);
  }, []);

  const onUpdateMessage = useCallback((messageId: number, updater: (message: Message) => Message) => {
    setMessages(prev => prev.map(msg => 
      msg.id === messageId ? updater(msg) : msg
    ));
  }, []);

  const onUpdateAllMessages = useCallback((updater: (messages: Message[]) => Message[]) => {
    setMessages(updater);
  }, []);

  const onClearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  const onMessagesLoaded = useCallback((loadedMessages: Message[]) => {
    setMessages(loadedMessages);
  }, []);

  // Chat history handlers
  const handleChatSessionSelect = useCallback(async (sessionId: string) => {
    router.push(`/chat?sessionId=${sessionId}`);
  }, [router]);

  const handleNewChat = useCallback(() => {
    router.push('/chat');
  }, [router]);

  const {
    selectedModel,
    setSelectedModel,
    selectedMode,
    setSelectedMode,
    selectedProviders,
    setSelectedProviders,
    isSending,
    setIsSending,
    handleSend,
    handlePromptClick,
  } = useChatSendHandlers({
    userId,
    currentSessionId,
    setCurrentSessionId,
    hasCreatedSession,
    setHasCreatedSession,
    createSession,
    router,
    onNewMessage,
    justCreatedSessionRef,
    onSessionCreated: refreshChatHistory,
    images,
  });

  const onSendingStateChange = useCallback((sending: boolean) => {
    setIsSending(sending);

    if (!sending && justCreatedSessionRef.current) {
      setTimeout(() => {
        justCreatedSessionRef.current = null;
      }, 100);
    }
  }, [setIsSending]);


  // Modular message handler
  const { handleWebSocketMessage } = useMessageHandler({
    streaming: streamingMessages,
    onNewMessage,
    onUpdateMessage,
    onSendingStateChange,
    isSending,
    onUpdateAllMessages,
    hasCreatedSession,
    justCreatedSessionRef,
    currentSessionId,
  });

  const onUiStateLoaded = useCallback((uiState: SimpleChatUiState) => {
    if (uiState.selectedModel) setSelectedModel(uiState.selectedModel);
    if (uiState.selectedMode) setSelectedMode(uiState.selectedMode);
    if (uiState.selectedProviders) setSelectedProviders(uiState.selectedProviders);
    if (uiState.input) setInput(uiState.input);
  }, [setSelectedModel, setSelectedMode, setSelectedProviders]);

  // Apply initial mode from URL parameter (e.g., from suggestion execution)
  useEffect(() => {
    if (initialMode) {
      setSelectedMode(initialMode);
    }
  }, [initialMode, setSelectedMode]);

  // Session loader for loading existing chat history
  const { loadSessionData, isLoadingSession } = useSessionLoader({
    onMessagesLoaded,
    onUiStateLoaded,
    onClearMessages,
  });

  // Note: UI state is now saved by the backend with each message
  // No need for frontend auto-save anymore

  // WebSocket integration
  const chatWebSocket = useWebSocket({
    url: getEnv('NEXT_PUBLIC_WEBSOCKET_URL') || '',
    userId: userId,
    onMessage: handleWebSocketMessage,
    onConnect: () => {
      console.log('Connected to chatbot WebSocket');
    },
    onDisconnect: () => {
      console.log('Chat WebSocket disconnected');
    },
    onError: (error) => {
      console.error('Chat WebSocket error:', error);
      setIsSending(false);
    }
  });

  const handleSendWithInput = useCallback(async () => {
    let finalMessage = input.trim();
    
    // Prepend incident context if present
    if (finalMessage && activeIncidentContext) {
      try {
        const ctx = JSON.parse(activeIncidentContext);
        const contextPrefix = `[INCIDENT CONTEXT]\nTitle: ${ctx.title ?? 'N/A'}\nSeverity: ${ctx.severity ?? 'N/A'}\nService: ${ctx.service ?? 'N/A'}\nSummary: ${ctx.summary ?? 'N/A'}\nRaw Alert: ${ctx.rawAlert ?? 'N/A'}\n\n[USER QUESTION]\n`;
        finalMessage = contextPrefix + finalMessage;
        setActiveIncidentContext(undefined); // Clear after first message
      } catch (e) {
        console.error('Failed to parse incident context:', e);
        setActiveIncidentContext(undefined);
      }
    }
    
    const sent = await handleSend(finalMessage || input, chatWebSocket);
    if (sent) {
      setInput("");
      setImages([]);
    }
  }, [chatWebSocket, handleSend, input, activeIncidentContext]);

  const handlePromptClickWithSocket = useCallback((prompt: string) => {
    setInput(prompt);
    handlePromptClick(prompt, chatWebSocket);
  }, [chatWebSocket, handlePromptClick]);

  // Chat cancellation functionality
  const { cancelCurrentMessage } = useChatCancellation({
    userId,
    sessionId: currentSessionId,
    webSocket: {
      isConnected: chatWebSocket.isConnected,
      send: chatWebSocket.send
    },
    wsRef: chatWebSocket.wsRef // Pass wsRef for better state checking
  });

  // Handle cancel button click
  const handleCancel = useCallback(async () => {
    try {
      await cancelCurrentMessage();
      // Reset sending state on frontend immediately
      setIsSending(false);
      // Stop any current streaming and preserve the message
      const finalMessage = finishStreamingMessage();
      if (finalMessage) {
        onNewMessage(finalMessage);
      }
    } catch (error) {
      console.error('Error cancelling message:', error);
    }
  }, [cancelCurrentMessage, finishStreamingMessage, onNewMessage]);

  // Reset streaming/sending state when switching sessions to avoid stale in-flight UI
  const previousSessionIdRef = useRef<string | null>(null);
  useEffect(() => {
    const previousSessionId = previousSessionIdRef.current;
    if (previousSessionId && previousSessionId !== currentSessionId) {
      if (checkIsStreaming()) {
        const finalMessage = finishStreamingMessage();
        if (finalMessage) {
          onNewMessage(finalMessage);
        }
      }
      cleanupStreaming();
      setIsSending(false);
      justCreatedSessionRef.current = null;
    }
    previousSessionIdRef.current = currentSessionId;
  }, [checkIsStreaming, cleanupStreaming, currentSessionId, finishStreamingMessage, setIsSending]);

  // Cleanup streaming timeout on unmount
  useEffect(() => {
    return () => {
      cleanupStreaming();
    };
  }, [cleanupStreaming]);

  // Initialize user and session
  useEffect(() => {
    if (!isLoaded) return;
    
    const initializeUserAndSession = async () => {
      let effectiveUserId: string;
      
      if (user) {
        effectiveUserId = user.id;
      } else {
        console.warn('[ChatClient] No authenticated user, redirecting to sign-in');
        router.replace('/sign-in');
        return;
      }
      
      setUserId(effectiveUserId);
    };
    
    initializeUserAndSession();
  }, [user, isLoaded]);

  // Load session once user is determined
  useEffect(() => {
    if (!userId || !initialSessionId) return;
    
    // Skip if we've already loaded this session
    if (lastLoadedSessionRef.current === initialSessionId) {
      return;
    }
    
    const loadInitialSession = async () => {
      const isNewSession = currentSessionId !== initialSessionId;
      
      // Only update state if actually different
      if (isNewSession || currentSessionId === null) {
        setCurrentSessionId(initialSessionId);
        setHasCreatedSession(true);
        lastLoadedSessionRef.current = initialSessionId;
        
        // Load session if not just created by us
        if (justCreatedSessionRef.current !== initialSessionId) {
          setIsLoadingSessionMessages(true);
          onClearMessages(); // Clear existing messages before loading new session
          try {
            const loaded = await loadSessionData(initialSessionId);
            if (!loaded) {
              console.warn(`Failed to load session ${initialSessionId}`);
              setHasCreatedSession(false);
            }
          } catch (error) {
            console.error('Error loading session:', error);
          } finally {
            setIsLoadingSessionMessages(false);
          }
        }
      }
    };
    
    loadInitialSession();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, initialSessionId]);

  // Register chat handlers with layout context
  useEffect(() => {
    setOnChatSessionSelect(() => handleChatSessionSelect);
    setOnNewChat(() => handleNewChat);
    setCurrentChatSessionId(currentSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleChatSessionSelect, handleNewChat, currentSessionId]);

  // Auto-send initial message from URL
  useEffect(() => {
    if (initialMessage && chatWebSocket.isReady && userId && !isLoadingSessionMessages && !isSending && !initialMessageSentRef.current) {
      initialMessageSentRef.current = true;
      const timer = setTimeout(() => {
        handleSend(initialMessage, chatWebSocket).then((success) => {
          if (success) {
            const sessionIdToUse = currentSessionId;
            window.history.replaceState({}, '', `/chat${sessionIdToUse ? `?sessionId=${sessionIdToUse}` : ''}`);
          }
        });
      }, 500);
      return () => clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialMessage, chatWebSocket.isReady, userId, isLoadingSessionMessages, isSending]);

  // Memoized message list
  const memoizedMessages = useMemo(() => {
    if (streamingMessages.currentStreamingMessage) {
      return [...messages, streamingMessages.currentStreamingMessage];
    }
    return messages;
  }, [messages, streamingMessages.currentStreamingMessage]);

  // Show loading state for user or session
  if (!isLoaded || isLoadingSessionMessages) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin" />
          {isLoadingSessionMessages && (
            <span className="text-sm text-muted-foreground">Loading conversation...</span>
          )}
          {!isLoaded && (
            <span className="text-sm text-muted-foreground">Loading user...</span>
          )}
        </div>
      </div>
    );
  }

  // Show empty state when no messages, otherwise show chat interface
  const hasMessages = memoizedMessages.length > 0;

  if (hasMessages) {
    // Standard chat interface with messages
    return (
      <div className="flex flex-col h-full w-full">

        {/* Messages */}
        <div className="flex-1 min-h-0 flex justify-center pt-6">
          <div className="w-full max-w-4xl">
            <VirtualizedMessages 
              messages={memoizedMessages} 
              sendRaw={chatWebSocket.sendRaw}
              onUpdateMessage={onUpdateMessage}
              sessionId={currentSessionId || undefined}
              userId={userId || undefined}
            />
          </div>
        </div>

        {/* Enhanced Input */}
        <div className="p-4 relative z-10 bg-background flex-shrink-0">
          <div className="flex justify-center">
            <EnhancedChatInput
              input={input}
              setInput={setInput}
              onSend={handleSendWithInput}
              isSending={isSending}
              selectedModel={selectedModel}
              onModelChange={setSelectedModel}
              selectedMode={selectedMode}
              onModeChange={setSelectedMode}
              selectedProviders={selectedProviders}
              placeholder="Ask anything..."
              onCancel={handleCancel}
              disabled={isSending}
              incidentContext={activeIncidentContext}
              onRemoveContext={() => setActiveIncidentContext(undefined)}
              images={images}
              onImagesChange={setImages}
            />
          </div>
        </div>
      </div>
    );
  }

  // Empty state with original interface design
  return (
    <div>

      {/* Empty state content */}
      <div className="flex-1 flex flex-col items-center justify-center min-h-0 py-8 pt-28">
        <div className="w-full max-w-5xl px-4 flex flex-col items-center mx-auto">
          <EmptyStateHeader />
          
          <EnhancedChatInput
            input={input}
            setInput={setInput}
            onSend={handleSendWithInput}
            isSending={isSending}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
            selectedMode={selectedMode}
            onModeChange={setSelectedMode}
            selectedProviders={selectedProviders}
            placeholder="Ask anything..."
            onCancel={handleCancel}
            disabled={isSending}
            incidentContext={activeIncidentContext}
            onRemoveContext={() => setActiveIncidentContext(undefined)}
            images={images}
            onImagesChange={setImages}
          />
          
          <div className="w-full max-w-3xl mt-6">
            <DynamicPrompts 
              onPromptClick={handlePromptClickWithSocket}
              className=""
            />
          </div>
        </div>
      </div>
    </div>
  );
}
