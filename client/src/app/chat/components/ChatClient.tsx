"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useUser, useAuth } from '@/hooks/useAuthHooks';
import { canWrite } from '@/lib/roles';
import { getEnv } from '@/lib/env';
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import dynamic from "next/dynamic";
// Core chat components
import { MessageList } from "@/components/chat/message-list";
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
import SessionUsagePanel from "@/components/SessionUsagePanel";
import { useSessionUsage } from '@/hooks/useSessionUsage';
import { useChatSendHandlers } from "./useChatSendHandlers";
import { useChatStream, type ChatRow } from '@/hooks/useChatStream';
import { useChatControl } from '@/hooks/useChatControl';
import type { TextPart, ToolPart } from '@/lib/chat-message-parts';
import type { ToolCall as LegacyToolCall } from '../types';

// Transport gate read at module load. `sse` routes through the SSE consumer +
// /api/chat/messages POST; anything else (default `ws`) keeps the legacy
// WebSocket path so a misconfig falls back to the safer transport.
const CHAT_TRANSPORT = (process.env.NEXT_PUBLIC_CHAT_TRANSPORT === 'sse') ? 'sse' : 'ws';

interface ChatClientProps {
  initialSessionId?: string;
  shouldStartNewChat?: boolean;
  initialMessage?: string;
  incidentContext?: string;
  initialMode?: string;
}

export default function ChatClient({ initialSessionId, shouldStartNewChat, initialMessage: initialMessageProp, incidentContext, initialMode }: ChatClientProps) {
  const { user, isLoaded } = useUser();
  const { role } = useAuth();
  const router = useRouter();
  
  // Resolve initial message from prop (URL) or sessionStorage (long commands).
  // We only read sessionStorage here; removal happens after the message is sent
  // to avoid React 18 StrictMode double-invocation clearing it prematurely.
  const [initialMessage] = useState<string | undefined>(() => {
    if (initialMessageProp) return initialMessageProp;
    if (typeof window !== 'undefined') {
      return sessionStorage.getItem('pendingChatMessage') ?? undefined;
    }
    return undefined;
  });
  
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

  // Session-level token usage tracking
  const sessionUsage = useSessionUsage(currentSessionId);

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
    onUsageUpdate: sessionUsage.handleUsageUpdate,
    onUsageFinal: sessionUsage.handleUsageFinal,
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
  const { loadSessionData } = useSessionLoader({
    onMessagesLoaded,
    onUiStateLoaded,
    onClearMessages,
  });

  // Note: UI state is now saved by the backend with each message
  // No need for frontend auto-save anymore

  // WebSocket integration — only active in legacy `ws` transport mode.
  const chatWebSocket = useWebSocket({
    url: CHAT_TRANSPORT === 'ws' ? (getEnv('NEXT_PUBLIC_WEBSOCKET_URL') || '') : '',
    userId: CHAT_TRANSPORT === 'ws' ? userId : null,
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
    },
  });

  // SSE transport. No-op when sessionId is null or the flag is `ws`.
  const chatStream = useChatStream({
    sessionId: currentSessionId,
    enabled: CHAT_TRANSPORT === 'sse',
  });
  const chatControl = useChatControl();

  // Bridge: when SSE is the active transport, mirror sseRows into the legacy
  // Message[] surface so existing <MessageList /> keeps rendering. We collapse
  // each row's text parts into a single text body and project tool parts into
  // toolCalls. data-subagent / data-plan / reasoning parts are rendered by
  // <MessagePartsRenderer /> in the incident <ThoughtsPanel /> — they are not
  // surfaced in the chat MessageList yet (TODO: phase out MessageList).
  useEffect(() => {
    if (CHAT_TRANSPORT !== 'sse') return;
    const rowToMessage = (row: ChatRow, idx: number): Message => {
      const text = row.parts
        .filter((p): p is TextPart => p.type === 'text')
        .map((p) => p.text)
        .join('');
      const toolCalls: LegacyToolCall[] = row.parts
        .filter((p): p is ToolPart => p.type.startsWith('tool-'))
        .map((tp) => {
          const toolName = tp.type.startsWith('tool-') ? tp.type.slice('tool-'.length) : tp.type;
          let status: LegacyToolCall['status'];
          switch (tp.state) {
            case 'output-available': status = 'completed'; break;
            case 'output-error': status = 'error'; break;
            default: status = 'running';
          }
          return {
            id: tp.toolCallId,
            tool_name: toolName,
            input: typeof tp.input === 'string' ? tp.input : JSON.stringify(tp.input ?? ''),
            output: tp.output,
            error: tp.errorText ?? null,
            status,
            timestamp: new Date().toISOString(),
          };
        });
      // Negative id flags this Message as SSE-sourced so the bridge can
      // distinguish it from session-loaded (positive id) and optimistic
      // (Date.now() id) messages. Stable across re-renders because firstSeq
      // is monotonic and unique per chat_events message.
      const sseId = -1 * (row.firstSeq || (idx + 1));
      return {
        id: sseId,
        sender: row.role === 'user' ? 'user' : 'bot',
        text,
        toolCalls: toolCalls.length ? toolCalls : undefined,
        isStreaming: row.status === 'streaming',
      };
    };
    const sseMessages = chatStream.rows.map(rowToMessage);
    if (sseMessages.length === 0) return;

    // Bridge SSE rows back into the legacy `messages` array.
    //
    // Keys to get right:
    //   1. session-loaded history (positive ids) must stay in place.
    //   2. the optimistic user message added by useChatSendHandlers
    //      (id=Date.now()) is a positive id that should be replaced once SSE
    //      delivers its own user_message row (negative id, same text).
    //   3. SSE-sourced messages (negative ids) are the live state for the
    //      current in-flight turn; we re-derive them from chatStream.rows on
    //      every render, so we drop prior SSE messages before re-appending.
    setMessages((prev) => {
      const nonSse = prev.filter((m) => m.id >= 0);
      const firstSseUser = sseMessages.find((m) => m.sender === 'user');
      let base = nonSse;
      if (firstSseUser) {
        let lastOptimisticUserIdx = -1;
        for (let i = nonSse.length - 1; i >= 0; i--) {
          if (nonSse[i].sender === 'user' && nonSse[i].text === firstSseUser.text) {
            lastOptimisticUserIdx = i;
            break;
          }
        }
        if (lastOptimisticUserIdx !== -1) {
          base = [
            ...nonSse.slice(0, lastOptimisticUserIdx),
            ...nonSse.slice(lastOptimisticUserIdx + 1),
          ];
        }
      }
      return [...base, ...sseMessages];
    });
  }, [chatStream.rows]);

  // Adapter: useChatSendHandlers expects a ChatWebSocket-shaped object. In
  // SSE mode we proxy `send` through POST /api/chat/messages and treat the
  // transport as always ready. This lets the rest of the send pipeline stay
  // identical across transports.
  const sseChatWebSocket = useMemo(() => ({
    send: (payload: any) => {
      if (!payload || payload.type !== 'message') return false;
      chatControl.sendMessage({
        session_id: payload.session_id,
        query: payload.query,
        mode: payload.mode,
        attachments: payload.attachments,
        model: payload.model,
        provider_preference: Array.isArray(payload.provider_preference)
          ? payload.provider_preference.join(',')
          : payload.provider_preference,
        ui_state: payload.ui_state,
      }).catch((err) => {
        console.error('[ChatClient] SSE sendMessage failed:', err);
        setIsSending(false);
      });
      return true;
    },
    isReady: true,
    isConnected: true,
  }), [chatControl, setIsSending]);

  const activeChatTransport = CHAT_TRANSPORT === 'sse' ? sseChatWebSocket : chatWebSocket;

  const [rcaActive, setRcaActive] = useState(false);

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
    
    const sent = await handleSend(finalMessage || input, activeChatTransport, undefined, rcaActive ? { triggerRca: true } : undefined);
    if (sent) {
      setInput("");
      setImages([]);
      setRcaActive(false);
    }
  }, [activeChatTransport, handleSend, input, activeIncidentContext, rcaActive]);

  const handlePromptClickWithSocket = useCallback((prompt: string) => {
    setInput(prompt);
    handlePromptClick(prompt, activeChatTransport);
  }, [activeChatTransport, handlePromptClick]);

  // Chat cancellation functionality (WebSocket only — in SSE mode, cancel is a control POST).
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
      if (CHAT_TRANSPORT === 'sse' && currentSessionId) {
        await chatControl.cancel(currentSessionId);
      } else {
        await cancelCurrentMessage();
      }
      setIsSending(false);
      sessionUsage.handleCancel();
      const finalMessage = finishStreamingMessage();
      if (finalMessage) {
        onNewMessage(finalMessage);
      }
    } catch (error) {
      console.error('Error cancelling message:', error);
    }
  }, [cancelCurrentMessage, chatControl, currentSessionId, finishStreamingMessage, onNewMessage, sessionUsage, setIsSending]);

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
    if (!userId) return;

    // New chat (no sessionId): clear previous session state
    if (!initialSessionId) {
      if (currentSessionId) {
        setCurrentSessionId(null);
        setHasCreatedSession(false);
        onClearMessages();
        lastLoadedSessionRef.current = null;
      }
      return;
    }
    
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

  // Auto-send initial message from URL or sessionStorage (e.g., Next Steps execution)
  useEffect(() => {
    if (initialMessage && activeChatTransport.isReady && userId && !isLoadingSessionMessages && !isSending && !initialMessageSentRef.current) {
      initialMessageSentRef.current = true;
      const timer = setTimeout(() => {
        handleSend(initialMessage, activeChatTransport).then((success) => {
          if (success) {
            sessionStorage.removeItem('pendingChatMessage');
            const sessionIdToUse = currentSessionId;
            window.history.replaceState({}, '', `/chat${sessionIdToUse ? `?sessionId=${sessionIdToUse}` : ''}`);
          } else {
            // Reset so the effect can retry when conditions are met again
            initialMessageSentRef.current = false;
          }
        });
      }, 500);
      return () => clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialMessage, activeChatTransport, activeChatTransport.isReady, userId, isLoadingSessionMessages, isSending]);

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

  // Viewers cannot interact with chat
  const isReadOnly = !canWrite(role);

  if (hasMessages) {
    // Standard chat interface with messages
    return (
      <div className="flex flex-col h-full w-full overflow-hidden">

        {/* Messages — full-width so scrollbar sits at page edge */}
        <div className="flex-1 min-h-0">
            <MessageList
              key={currentSessionId || "new"}
              messages={memoizedMessages} 
              sendRaw={CHAT_TRANSPORT === 'sse' ? ((data: string) => {
                try {
                  const parsed = JSON.parse(data);
                  if (parsed?.direct_tool_call && currentSessionId) {
                    chatControl.triggerDirectTool(currentSessionId, parsed.direct_tool_call)
                      .catch((err) => console.error('[ChatClient] direct-tool failed:', err));
                    return true;
                  }
                } catch (err) {
                  console.error('[ChatClient] sendRaw parse failed:', err);
                }
                return false;
              }) : chatWebSocket.sendRaw}
              onUpdateMessage={onUpdateMessage}
              sessionId={currentSessionId || undefined}
              userId={userId || undefined}
            />
        </div>

        {/* Enhanced Input */}
        <div className="p-4 relative z-10 bg-background flex-shrink-0">
          <div className="max-w-4xl mx-auto space-y-2">
            <SessionUsagePanel sessionUsage={sessionUsage} isSending={isSending} />
            {isReadOnly ? (
              <p className="text-sm text-muted-foreground py-2">Read-only access. Editors and admins can interact with infrastructure.</p>
            ) : (
            <EnhancedChatInput
              input={input}
              setInput={setInput}
              onSend={handleSendWithInput}
              rcaActive={rcaActive}
              onToggleRCA={() => setRcaActive(prev => !prev)}
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
            )}
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
          
          {isReadOnly ? (
            <p className="text-sm text-muted-foreground py-4">Read-only access. Editors and admins can interact with infrastructure.</p>
          ) : (
          <>
          <EnhancedChatInput
            input={input}
            setInput={setInput}
            onSend={handleSendWithInput}
            rcaActive={rcaActive}
            onToggleRCA={() => setRcaActive(prev => !prev)}
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
          </>
          )}
        </div>
      </div>
    </div>
  );
}
