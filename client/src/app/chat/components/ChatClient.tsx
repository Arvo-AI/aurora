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
import { useToast } from '@/hooks/use-toast';
import { ToastAction } from '@/components/ui/toast';
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
  const { toast } = useToast();
  
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
    // Optimistic bubble bridges the ~200–500ms gap between POST kickoff and
    // SSE delivering user_message; the bridge collapses it on match.
    skipOptimisticUserMessage: false,
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
  // onMetaCompleted clears isSending: SSE has no equivalent of the WS END
  // status that useMessageHandler hooks into, so without this the input
  // stays disabled after the first reply.
  const chatStream = useChatStream({
    sessionId: currentSessionId,
    enabled: CHAT_TRANSPORT === 'sse',
    onMetaCompleted: useCallback(() => {
      setIsSending(false);
    }, []),
    onUsageUpdate: sessionUsage.handleUsageUpdate,
    onUsageFinal: sessionUsage.handleUsageFinal,
    onToast: useCallback((payload: Record<string, unknown>) => {
      const action = payload.action as { label?: string; onClick?: string } | undefined;
      const variant = (payload.variant as 'default' | 'destructive' | undefined) ?? 'default';
      const duration = typeof payload.duration === 'number' ? payload.duration : undefined;
      // The backend speaks in symbolic onClick handles (e.g. "open_connectors")
      // because the toast envelope crosses the wire. Resolve known handles to
      // a router.push; unknown handles fall through with no action button.
      let toastAction: React.ReactElement<typeof ToastAction> | undefined;
      if (action?.label && action?.onClick === 'open_connectors') {
        const label = action.label;
        toastAction = (
          <ToastAction altText={label} onClick={() => router.push('/connectors')}>
            {label}
          </ToastAction>
        );
      }
      toast({
        title: typeof payload.title === 'string' ? payload.title : undefined,
        description: typeof payload.description === 'string' ? payload.description : undefined,
        variant,
        duration,
        ...(toastAction ? { action: toastAction } : {}),
      });
    }, [toast, router]),
  });
  const chatControl = useChatControl();

  // Bridge: when SSE is the active transport, mirror sseRows into the legacy
  // Message[] surface so existing <MessageList /> keeps rendering. Each
  // assistant row is split at tool-call boundaries into one Message per text
  // segment, matching how chat_messages persists pre-tool/post-tool text as
  // separate rows on refresh; tool parts attach as toolCalls to the segment
  // they followed. data-subagent / data-plan / reasoning parts are rendered
  // by <MessagePartsRenderer /> in the incident <ThoughtsPanel />.
  useEffect(() => {
    if (CHAT_TRANSPORT !== 'sse') return;
    const toolPartToLegacy = (tp: ToolPart): LegacyToolCall => {
      const toolName = tp.type.startsWith('tool-') ? tp.type.slice('tool-'.length) : tp.type;
      let status: LegacyToolCall['status'];
      switch (tp.state) {
        case 'output-available': status = 'completed'; break;
        case 'output-error': status = 'error'; break;
        case 'awaiting-confirmation': status = 'awaiting_confirmation'; break;
        case 'setting-up-environment': status = 'setting_up_environment'; break;
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
        confirmation_id: tp.confirmationId,
        confirmation_message: tp.confirmationMessage,
      };
    };
    // Negative ids flag SSE-sourced rows so the bridge can distinguish them
    // from session-loaded (string UUIDs) and optimistic (positive Date.now())
    // messages. firstSeq is monotonic and unique per chat_events message;
    // segments within a single row stay stable by offsetting from firstSeq.
    const rowToMessages = (row: ChatRow, idx: number): Message[] => {
      const baseId = -1 * (row.firstSeq || (idx + 1));
      const sender = row.role === 'user' ? 'user' : 'bot';
      const isStreaming = row.status === 'streaming';
      if (sender === 'user') {
        const text = row.parts
          .filter((p): p is TextPart => p.type === 'text')
          .map((p) => p.text)
          .join('');
        return [{ id: baseId, sender, text, isStreaming }];
      }
      const segments: Message[] = [];
      let currentText = '';
      let pendingTools: LegacyToolCall[] = [];
      let segmentIdx = 0;
      const flush = () => {
        if (!currentText && pendingTools.length === 0) return;
        segments.push({
          id: baseId - segmentIdx,
          sender,
          text: currentText,
          toolCalls: pendingTools.length ? pendingTools : undefined,
          isStreaming,
        });
        segmentIdx += 1;
        currentText = '';
        pendingTools = [];
      };
      // Split at every tool-call boundary so each text run between tools is
      // its own bubble. Pre-refresh boundary lands wherever the LLM stream
      // happened to be (often mid-word, since tool calls fire mid-token);
      // post-refresh splits at AIMessage boundaries which the raw stream
      // doesn't expose, so the two views can differ on the exact byte break.
      for (const part of row.parts) {
        if (part.type === 'text') {
          if (pendingTools.length && currentText) flush();
          currentText += part.text;
        } else if (part.type.startsWith('tool-')) {
          pendingTools.push(toolPartToLegacy(part as ToolPart));
        }
      }
      flush();
      if (segments.length === 0) {
        segments.push({ id: baseId, sender, text: '', isStreaming });
      }
      return segments;
    };
    // Backend emits two user_message events per turn (chat_sse POST and the
    // workflow's immediate_save_handler under a fresh message_id), so collapse
    // same-text user rows down to the first occurrence.
    const seenUserText = new Set<string>();
    const sseMessages = chatStream.rows
      .flatMap(rowToMessages)
      .filter((m) => {
        if (m.sender !== 'user') return true;
        if (seenUserText.has(m.text)) return false;
        seenUserText.add(m.text);
        return true;
      });
    if (sseMessages.length === 0) return;

    // SSE rows carry negative numeric ids; positive numeric ids are the
    // optimistic Date.now() bubble; strings are session-loaded UUIDs. Drop
    // the prior SSE batch, swap out any optimistic that the SSE user_message
    // now matches, and re-append the freshly-derived rows.
    setMessages((prev) => {
      let nonSse = prev.filter((m) => typeof m.id !== 'number' || m.id >= 0);
      for (const sseUser of sseMessages) {
        if (sseUser.sender !== 'user') continue;
        let optimisticIdx = -1;
        for (let i = nonSse.length - 1; i >= 0; i--) {
          const m = nonSse[i];
          if (
            typeof m.id === 'number' &&
            m.id > 0 &&
            m.sender === 'user' &&
            m.text === sseUser.text
          ) {
            optimisticIdx = i;
            break;
          }
        }
        if (optimisticIdx !== -1) {
          nonSse = [
            ...nonSse.slice(0, optimisticIdx),
            ...nonSse.slice(optimisticIdx + 1),
          ];
        }
      }
      return [...nonSse, ...sseMessages];
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
        // POST failed so the SSE bridge will never see a matching
        // user_message; drop the orphan optimistic so it doesn't strand at
        // the top of the list while later turns get appended below.
        const failedQuery: string | undefined = payload?.query;
        if (typeof failedQuery === 'string') {
          setMessages((prev) => {
            for (let i = prev.length - 1; i >= 0; i--) {
              const m = prev[i];
              if (
                typeof m.id === 'number' &&
                m.id > 0 &&
                m.sender === 'user' &&
                m.text === failedQuery
              ) {
                return [...prev.slice(0, i), ...prev.slice(i + 1)];
              }
            }
            return prev;
          });
        }
        toast({
          description: "Couldn't send your message. Please try again.",
          variant: "destructive",
        });
      });
      return true;
    },
    isReady: true,
    isConnected: true,
  }), [chatControl, setIsSending, toast]);

  const activeChatTransport = CHAT_TRANSPORT === 'sse' ? sseChatWebSocket : chatWebSocket;

  // SSE Confirm/Decline → POST /api/chat/confirmations. WS mode leaves this
  // undefined so the widget falls back to its sendRaw frame.
  const onSseConfirm = useCallback(
    async (confirmationId: string, decision: 'approve' | 'decline') => {
      if (!currentSessionId) return;
      try {
        await chatControl.respondToConfirmation(currentSessionId, confirmationId, decision);
      } catch (err) {
        console.error('[ChatClient] respondToConfirmation failed:', err);
        toast({
          description: "Couldn't send your response. Please try again.",
          variant: 'destructive',
        });
      }
    },
    [chatControl, currentSessionId, toast],
  );

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
              onConfirm={CHAT_TRANSPORT === 'sse' ? onSseConfirm : undefined}
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
