"use client";

import { useCallback, useEffect, useRef } from "react";
import { Message, ToolCall } from "../app/chat/types";
import { WebSocketMessage } from "./useWebSocket";
import { StreamingMessageState } from "./useStreamingMessages";
import { useChatExpansion } from "../app/components/ClientShell";
import { generateUniqueId, generateNumericId } from "../utils/idGenerator";

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
}

export const useMessageHandler = ({
  streaming,
  onNewMessage,
  onUpdateMessage,
  onSendingStateChange,
  isSending,
  onUpdateAllMessages,
  hasCreatedSession = false,
  justCreatedSessionRef,
  currentSessionId
}: UseMessageHandlerProps) => {
  // Store tool call message IDs for updates
  const toolCallMessageIds = useRef<Map<string, number>>(new Map());
  
  // Track whether we've already refreshed for this session
  const hasRefreshedForSessionRef = useRef<string | null>(null);
  
  // Get chat history refresh function
  const { refreshChatHistory } = useChatExpansion();

  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    // CRITICAL FIX: Filter messages by session_id to prevent cross-conversation leakage
    // When a session is active, ONLY process messages that match the current session
    if (currentSessionId) {
      // Filter out messages without session_id OR with non-matching session_id
      if (!message.session_id || message.session_id !== currentSessionId) {
        // Log for debugging (only for mismatched sessions, not missing session_id)
        if (message.session_id && message.session_id !== currentSessionId) {
          console.debug('[useMessageHandler] Filtered message from different session:', {
            messageSession: message.session_id,
            currentSession: currentSessionId,
            type: message.type
          });
        }
        return;
      }
    }

    // Handle tool status updates (e.g., "setting_up_environment")
    if (message.type === 'tool_status' && message.data) {
      const status = message.data.status;
      
      if (status === 'setting_up_environment' && onUpdateAllMessages) {
        // Find the most recent running tool call and update its status
        onUpdateAllMessages(prev => {
          const messages = [...prev];
          for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].toolCalls) {
              let foundMatch = false;
              const updatedToolCalls = messages[i].toolCalls!.map(tc => {
                if (tc.status === 'running' && !foundMatch) {
                  foundMatch = true;
                  return {
                    ...tc,
                    status: 'setting_up_environment' as const
                  };
                }
                return tc;
              });
              
              if (foundMatch) {
                messages[i] = { ...messages[i], toolCalls: updatedToolCalls };
                break;
              }
            }
          }
          return messages;
        });
      }
      return;
    }
    
    // Handle execution confirmation requests
    if (message.type === 'execution_confirmation' && message.data) {
      const toolName = message.data.tool_name;
      const confirmationId = message.data.confirmation_id;
      
      // Find the most recent running tool call for this tool and update its status
      if (toolName && onUpdateAllMessages) {
        onUpdateAllMessages(prev => {
          // Find the last message with a matching running tool call
          const messages = [...prev];
          for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].toolCalls) {
              let foundMatch = false;
              const updatedToolCalls = messages[i].toolCalls!.map(tc => {
                if (tc.tool_name === toolName && tc.status === 'running' && !foundMatch) {
                  foundMatch = true;
                  return {
                    ...tc,
                    status: 'awaiting_confirmation' as const,
                    confirmation_id: confirmationId,
                    confirmation_message: message.data.message
                  };
                }
                return tc;
              });
              
              if (foundMatch) {
                messages[i] = { ...messages[i], toolCalls: updatedToolCalls };
                break;
              }
            }
          }
          return messages;
        });
      }
      return;
    }

    if (message.type === 'error' && message.data?.code === 'READ_ONLY_MODE') {
      const errorText = message.data.text || 'This action is unavailable in read-only mode.';
      onNewMessage({
        id: generateNumericId(),
        sender: 'bot',
        text: errorText,
      });
      onSendingStateChange(false);
      return;
    }

    // Handle tool calls - create a separate message immediately
    if (message.type === 'tool_call' && message.data) {
      //console.log('Tool call received:', message.data);
      
      // CRITICAL FIX: Finalize any streaming text BEFORE adding a new tool call
      // This preserves chronological order when the agent sends text then invokes a tool
      if (streaming.checkIsStreaming()) {
        const finalMessage = streaming.finishStreamingMessage();
        if (finalMessage) {
          onNewMessage(finalMessage);
        }
      }
      
      // Use backend's tool_call_id if provided, otherwise generate a collision-resistant fallback
      // The fallback ID should be echoed back if the backend accepts it
      const toolCallId = message.data.tool_call_id || `tool-${generateUniqueId()}`;
      
      const toolCall: ToolCall = {
        id: toolCallId,
        tool_name: message.data.tool_name,
        input: message.data.input,
        status: message.data.status || 'running',
        timestamp: message.data.timestamp || new Date().toISOString()
      };
      
      // Create a separate message for tool calls immediately
      // Use collision-resistant numeric ID instead of Date.now()
      const messageId = generateNumericId();
      const toolCallMessage: Message = {
        id: messageId,
        sender: "bot",
        text: "", // Empty text for tool call messages
        toolCalls: [toolCall]
      };
      
      // Store the message ID for later updates using tool_call_id
      toolCallMessageIds.current.set(toolCall.id, messageId);
      
      onNewMessage(toolCallMessage);
    }
    
    // Handle tool results
    if (message.type === 'tool_result' && message.data) {
      //console.log('Tool result received:', message.data);
      const toolCallId = message.data.tool_call_id;
      const toolName = message.data.tool_name;
      const output = message.data.output;
      
      // Find the message ID for this tool call using tool_call_id
      const messageId = toolCallMessageIds.current.get(toolCallId);
      if (messageId) {
        // Update the existing message with the tool result
        onUpdateMessage(messageId, (msg) => ({
          ...msg,
          toolCalls: msg.toolCalls?.map(toolCall => 
            toolCall.id === toolCallId 
              ? { ...toolCall, output, status: 'completed' as const }
              : toolCall
          ) || []
        }));
        
        // Clean up the stored message ID
        toolCallMessageIds.current.delete(toolCallId);
      }
    }
    
    // Handle thinking content from Gemini thinking models
    // Thinking messages are streamed separately and displayed with distinct styling
    if (message.type === 'thinking' && message.data) {
      const thinkingText = message.data.text || message.data;
      const isChunk = message.data.is_chunk || false;
      const isStreamingFlag = message.data.streaming || false;
      
      // Stream thinking content just like regular messages but with thinking flag
      if (isChunk || isStreamingFlag) {
        if (!streaming.checkIsStreaming()) {
          streaming.startStreamingMessage(true); // Pass true to indicate thinking content
        }
        streaming.appendToStreamingMessage(thinkingText);
      } else {
        // Complete thinking message
        if (streaming.checkIsStreaming()) {
          streaming.appendToStreamingMessage(thinkingText);
          const finalMessage = streaming.finishStreamingMessage();
          if (finalMessage) {
            onNewMessage({ ...finalMessage, isThinking: true });
          }
        } else {
          onNewMessage({
            id: generateNumericId(),
            sender: "bot",
            text: thinkingText,
            isThinking: true, // Mark as thinking content for special display
          });
        }
      }
      return;
    }
    
    // Handle different message types for proper streaming
    if (message.type === 'message' && message.data) {
      const messageText = message.data.text || message.data;
      const isChunk = message.data.is_chunk || false;
      const isComplete = message.data.is_complete || false;
      const isStreamingFlag = message.data.streaming || false;
      
      // Trigger refresh ONLY on the FIRST message of a NEW chat session
      // Check if: 1) This is a chunk, 2) Not currently streaming, 3) Session was just created
      const currentSessionId = justCreatedSessionRef?.current;
      const shouldRefresh = isChunk && 
                           !streaming.checkIsStreaming() && 
                           messageText && 
                           currentSessionId && 
                           hasRefreshedForSessionRef.current !== currentSessionId;
      
      if (shouldRefresh) {
        hasRefreshedForSessionRef.current = currentSessionId;
        setTimeout(() => {
          refreshChatHistory();
        }, 2000); // Delay to ensure backend has finished all updates
      }
      
      // Pattern 1: Explicit chunk/complete flags
      if (isChunk && !isComplete) {
        if (!streaming.checkIsStreaming()) {
          streaming.startStreamingMessage();
        }
        streaming.appendToStreamingMessage(messageText);
      } 
      else if (isComplete) {
        if (streaming.checkIsStreaming()) {
          streaming.appendToStreamingMessage(messageText);
          const finalMessage = streaming.finishStreamingMessage();
          if (finalMessage) {
            onNewMessage(finalMessage);
          }
        } else {
          // Complete message received without streaming
          const completeMessage: Message = {
            id: generateNumericId(),
            sender: "bot",
            text: messageText,
          };
          onNewMessage(completeMessage);
          onSendingStateChange(false);
        }
      }
      // Pattern 2: Streaming flag
      else if (isStreamingFlag) {
        if (!streaming.checkIsStreaming()) {
          streaming.startStreamingMessage();
        }
        streaming.appendToStreamingMessage(messageText);
      }
      // Pattern 3: Auto-detect streaming by message length/content
      else if (typeof messageText === 'string' && messageText.length < 50 && messageText.length > 0) {
        // Likely a streaming chunk (short text)
        if (!streaming.checkIsStreaming() && isSending) {
          streaming.startStreamingMessage();
        }
        if (streaming.checkIsStreaming()) {
          streaming.appendToStreamingMessage(messageText);
        } else {
          // Complete short message
          onNewMessage({
            id: generateNumericId(),
            sender: "bot",
            text: messageText,
          });
          onSendingStateChange(false);
        }
      }
      // Pattern 4: Complete message (default)
      else {
        // Finish any streaming message first
        if (streaming.checkIsStreaming()) {
          const finalMessage = streaming.finishStreamingMessage();
          if (finalMessage) {
            onNewMessage(finalMessage);
          }
        }
        
        const newMessage: Message = {
          id: generateNumericId(),
          sender: "bot",
          text: messageText,
        };
        onNewMessage(newMessage);
        onSendingStateChange(false);
      }
    }
    
    // Handle special completion signal
    // Note: 'status' messages can be START or END, so we check for isComplete or END status
    const isCompletionSignal = 
      (message.type as string) === 'complete' || 
      (message.type as string) === 'finished' ||
      (message.type as string) === 'usage_info' ||
      ((message.type as string) === 'status' && (message.isComplete || message.data?.status === 'END'));
    
    if (isCompletionSignal) {
      // Finish any streaming message first
      if (streaming.checkIsStreaming()) {
        const finalMessage = streaming.finishStreamingMessage();
        if (finalMessage) {
          onNewMessage(finalMessage);
        }
      }
      // Always clear the sending state when we receive any completion signal
      onSendingStateChange(false);
    }
  }, [streaming, onNewMessage, onUpdateMessage, onSendingStateChange, isSending, refreshChatHistory, justCreatedSessionRef, currentSessionId]);

  // Clear tool call message IDs when switching sessions to prevent cross-session contamination
  useEffect(() => {
    toolCallMessageIds.current.clear();
  }, [currentSessionId]);

  return {
    handleWebSocketMessage
  };
};
