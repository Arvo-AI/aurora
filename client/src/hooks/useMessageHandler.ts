"use client";

import { useCallback, useEffect, useRef } from "react";
import { Message, ToolCall } from "../app/chat/types";
import { WebSocketMessage } from "./useWebSocket";
import { StreamingMessageState } from "./useStreamingMessages";
import { useChatExpansion } from "../app/components/ClientShell";
import { generateUniqueId, generateNumericId } from "../utils/idGenerator";

// Multi-agent event types emitted alongside the existing envelope.
// Consumers (e.g., the incident page) can subscribe via `onSubAgentEvent`.
export type SubAgentEventType =
  | 'subagent_dispatched'
  | 'subagent_finished'
  | 'subagent_failed'
  | 'plan_committed';

export interface SubAgentEvent {
  type: SubAgentEventType;
  payload: Record<string, unknown>;
}

interface UseMessageHandlerProps {
  streaming: StreamingMessageState;
  onNewMessage: (message: Message) => void;
  onUpdateMessage: (messageId: number, updater: (message: Message) => Message) => void;
  onSendingStateChange: (isSending: boolean) => void;
  isSending: boolean;
  onUpdateAllMessages?: (updater: (messages: Message[]) => Message[]) => void;
  hasCreatedSession?: boolean;
  justCreatedSessionRef?: React.MutableRefObject<string | null>;
  currentSessionId: string | null;
  onUsageUpdate?: (data: Record<string, unknown>) => void;
  onUsageFinal?: (data: Record<string, unknown>) => void;
  // Multi-agent: optional subscriber for sub-agent lifecycle events.
  // If absent, events are silently dropped (forward-compatible no-op).
  onSubAgentEvent?: (evt: SubAgentEvent) => void;
}

// Forward-compatible WebSocket message dispatcher.
// All branches are explicit; unknown event types fall through to the default
// no-op and never throw, so the server can ship new events without breaking
// the client.
export const useMessageHandler = ({
  streaming,
  onNewMessage,
  onUpdateMessage,
  onSendingStateChange,
  isSending,
  onUpdateAllMessages,
  justCreatedSessionRef,
  currentSessionId,
  onUsageUpdate,
  onUsageFinal,
  onSubAgentEvent,
}: UseMessageHandlerProps) => {
  // Tool-call message id map — survives across renders, cleared on session change.
  const toolCallMessageIds = useRef<Map<string, number>>(new Map());
  const hasRefreshedForSessionRef = useRef<string | null>(null);
  const { refreshChatHistory } = useChatExpansion();

  const finishStreamIfActive = useCallback(() => {
    if (streaming.checkIsStreaming()) {
      const finalMessage = streaming.finishStreamingMessage();
      if (finalMessage) onNewMessage(finalMessage);
    }
  }, [streaming, onNewMessage]);

  const handleToolStatus = useCallback((message: WebSocketMessage) => {
    const status = message.data?.status;
    if (status !== 'setting_up_environment' || !onUpdateAllMessages) return;
    onUpdateAllMessages((prev) => {
      const messages = [...prev];
      for (let i = messages.length - 1; i >= 0; i--) {
        if (!messages[i].toolCalls) continue;
        let foundMatch = false;
        const updatedToolCalls = messages[i].toolCalls!.map((tc) => {
          if (tc.status === 'running' && !foundMatch) {
            foundMatch = true;
            return { ...tc, status: 'setting_up_environment' as const };
          }
          return tc;
        });
        if (foundMatch) {
          messages[i] = { ...messages[i], toolCalls: updatedToolCalls };
          break;
        }
      }
      return messages;
    });
  }, [onUpdateAllMessages]);

  const handleExecutionConfirmation = useCallback((message: WebSocketMessage) => {
    const toolName = message.data?.tool_name;
    const confirmationId = message.data?.confirmation_id;
    if (!toolName || !onUpdateAllMessages) return;
    onUpdateAllMessages((prev) => {
      const messages = [...prev];
      for (let i = messages.length - 1; i >= 0; i--) {
        if (!messages[i].toolCalls) continue;
        let foundMatch = false;
        const updatedToolCalls = messages[i].toolCalls!.map((tc) => {
          if (tc.tool_name === toolName && tc.status === 'running' && !foundMatch) {
            foundMatch = true;
            return {
              ...tc,
              status: 'awaiting_confirmation' as const,
              confirmation_id: confirmationId,
              confirmation_message: message.data?.message,
            };
          }
          return tc;
        });
        if (foundMatch) {
          messages[i] = { ...messages[i], toolCalls: updatedToolCalls };
          break;
        }
      }
      return messages;
    });
  }, [onUpdateAllMessages]);

  const handleError = useCallback((message: WebSocketMessage) => {
    if (message.data?.code === 'READ_ONLY_MODE') {
      const errorText = message.data.text || 'This action is unavailable in read-only mode.';
      onNewMessage({ id: generateNumericId(), sender: 'bot', text: errorText });
      onSendingStateChange(false);
      return;
    }
    finishStreamIfActive();
    const errorText =
      message.data?.text || message.data?.message || 'An unexpected error occurred.';
    onNewMessage({
      id: generateNumericId(),
      sender: 'bot',
      text: `⚠️ ${errorText}`,
      severity: 'error',
    });
    onSendingStateChange(false);
  }, [finishStreamIfActive, onNewMessage, onSendingStateChange]);

  const handleToolCall = useCallback((message: WebSocketMessage) => {
    if (!message.data) return;
    finishStreamIfActive();
    const toolCallId = message.data.tool_call_id || `tool-${generateUniqueId()}`;
    const toolCall: ToolCall = {
      id: toolCallId,
      tool_name: message.data.tool_name,
      input: message.data.input,
      status: message.data.status || 'running',
      timestamp: message.data.timestamp || new Date().toISOString(),
    };
    const messageId = generateNumericId();
    toolCallMessageIds.current.set(toolCall.id, messageId);
    onNewMessage({ id: messageId, sender: 'bot', text: '', toolCalls: [toolCall] });
  }, [finishStreamIfActive, onNewMessage]);

  const handleToolResult = useCallback((message: WebSocketMessage) => {
    if (!message.data) return;
    const toolCallId = message.data.tool_call_id;
    const output = message.data.output;
    const messageId = toolCallMessageIds.current.get(toolCallId);
    if (!messageId) return;
    onUpdateMessage(messageId, (msg) => ({
      ...msg,
      toolCalls:
        msg.toolCalls?.map((tc) =>
          tc.id === toolCallId ? { ...tc, output, status: 'completed' as const } : tc,
        ) || [],
    }));
    toolCallMessageIds.current.delete(toolCallId);
  }, [onUpdateMessage]);

  const handleThinking = useCallback((message: WebSocketMessage) => {
    if (!message.data) return;
    const thinkingText = message.data.text || message.data;
    const isChunk = message.data.is_chunk || false;
    const isStreamingFlag = message.data.streaming || false;
    if (isChunk || isStreamingFlag) {
      if (!streaming.checkIsStreaming()) streaming.startStreamingMessage(true);
      streaming.appendToStreamingMessage(thinkingText);
      return;
    }
    if (streaming.checkIsStreaming()) {
      streaming.appendToStreamingMessage(thinkingText);
      const finalMessage = streaming.finishStreamingMessage();
      if (finalMessage) onNewMessage({ ...finalMessage, isThinking: true });
    } else {
      onNewMessage({
        id: generateNumericId(),
        sender: 'bot',
        text: thinkingText,
        isThinking: true,
      });
    }
  }, [streaming, onNewMessage]);

  const handleAssistantMessage = useCallback((message: WebSocketMessage) => {
    if (!message.data) return;
    const messageText = message.data.text || message.data;
    const isChunk = message.data.is_chunk || false;
    const isComplete = message.data.is_complete || false;
    const isStreamingFlag = message.data.streaming || false;

    // Refresh chat history once per newly-created session, after first chunk arrives.
    const newSessionId = justCreatedSessionRef?.current;
    if (
      isChunk &&
      !streaming.checkIsStreaming() &&
      messageText &&
      newSessionId &&
      hasRefreshedForSessionRef.current !== newSessionId
    ) {
      hasRefreshedForSessionRef.current = newSessionId;
      setTimeout(() => refreshChatHistory(), 2000);
    }

    if (isChunk && !isComplete) {
      if (!streaming.checkIsStreaming()) streaming.startStreamingMessage();
      streaming.appendToStreamingMessage(messageText);
      return;
    }
    if (isComplete) {
      if (streaming.checkIsStreaming()) {
        streaming.appendToStreamingMessage(messageText);
        const finalMessage = streaming.finishStreamingMessage();
        if (finalMessage) onNewMessage(finalMessage);
      } else {
        onNewMessage({ id: generateNumericId(), sender: 'bot', text: messageText });
        onSendingStateChange(false);
      }
      return;
    }
    if (isStreamingFlag) {
      if (!streaming.checkIsStreaming()) streaming.startStreamingMessage();
      streaming.appendToStreamingMessage(messageText);
      return;
    }
    // Auto-detect short streaming chunk
    if (typeof messageText === 'string' && messageText.length < 50 && messageText.length > 0) {
      if (!streaming.checkIsStreaming() && isSending) streaming.startStreamingMessage();
      if (streaming.checkIsStreaming()) {
        streaming.appendToStreamingMessage(messageText);
      } else {
        onNewMessage({ id: generateNumericId(), sender: 'bot', text: messageText });
        onSendingStateChange(false);
      }
      return;
    }
    finishStreamIfActive();
    onNewMessage({ id: generateNumericId(), sender: 'bot', text: messageText });
    onSendingStateChange(false);
  }, [streaming, justCreatedSessionRef, refreshChatHistory, isSending, onNewMessage, onSendingStateChange, finishStreamIfActive]);

  const handleCompletion = useCallback(() => {
    finishStreamIfActive();
    onSendingStateChange(false);
  }, [finishStreamIfActive, onSendingStateChange]);

  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    // Cross-session leakage guard.
    if (currentSessionId && message.session_id && message.session_id !== currentSessionId) {
      console.debug('[useMessageHandler] Filtered cross-session message', {
        messageSession: message.session_id,
        currentSession: currentSessionId,
        type: message.type,
      });
      return;
    }
    if (currentSessionId && !message.session_id) return;

    const type = message.type as string;

    // Multi-agent lifecycle (optional subscriber).
    if (
      type === 'subagent_dispatched' ||
      type === 'subagent_finished' ||
      type === 'subagent_failed' ||
      type === 'plan_committed'
    ) {
      onSubAgentEvent?.({
        type: type as SubAgentEventType,
        payload: (message.data ?? {}) as Record<string, unknown>,
      });
      return;
    }

    // Completion signals — handled before the main switch because `status` has
    // a special END semantic.
    const isCompletionSignal =
      type === 'complete' ||
      type === 'finished' ||
      type === 'usage_info' ||
      (type === 'status' && (message.isComplete || message.data?.status === 'END'));
    if (isCompletionSignal) {
      handleCompletion();
      return;
    }

    switch (type) {
      case 'tool_status':
        handleToolStatus(message);
        return;
      case 'execution_confirmation':
        handleExecutionConfirmation(message);
        return;
      case 'error':
        handleError(message);
        return;
      case 'tool_call':
        handleToolCall(message);
        return;
      case 'tool_result':
        handleToolResult(message);
        return;
      case 'thinking':
        handleThinking(message);
        return;
      case 'message':
        handleAssistantMessage(message);
        return;
      case 'usage_update':
        if (message.data) onUsageUpdate?.(message.data);
        return;
      case 'usage_final':
        if (message.data) onUsageFinal?.(message.data);
        return;
      default:
        // Forward-compatible: unknown types are ignored.
        return;
    }
  }, [
    currentSessionId, onSubAgentEvent, handleCompletion, handleToolStatus,
    handleExecutionConfirmation, handleError, handleToolCall, handleToolResult,
    handleThinking, handleAssistantMessage, onUsageUpdate, onUsageFinal,
  ]);

  // Clear tool call message IDs when switching sessions to prevent cross-session contamination.
  useEffect(() => {
    toolCallMessageIds.current.clear();
  }, [currentSessionId]);

  return { handleWebSocketMessage };
};
