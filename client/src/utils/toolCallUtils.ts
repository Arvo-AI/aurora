/**
 * Utility functions for handling tool calls in the chat interface
 * Clean, modular approach to tool call detection and management
 */

import { Message } from '@/app/chat/types';

/**
 * Check if any messages have tool calls that might still be processing
 * This helps prevent auto-save from interfering with ongoing tool calls
 */
export const hasActiveToolCalls = (messages: Message[]): boolean => {
  return messages.some(message => {
    // Check if message has tool calls
    if (!message.toolCalls?.length) return false;
    
    // Check if any tool calls are still running or pending
    return message.toolCalls.some(toolCall => 
      toolCall.status === 'running' || 
      toolCall.status === 'pending'
    );
  });
};

/**
 * Check if the chat is currently receiving streaming content
 * This indicates that tool calls might be in progress
 */
export const hasStreamingContent = (messages: Message[]): boolean => {
  return messages.some(message => message.isStreaming === true);
};

/**
 * Determine if auto-save should be delayed due to tool call activity
 * Combines multiple indicators to make a smart decision
 */
export const shouldDelayAutoSave = (messages: Message[]): boolean => {
  return hasActiveToolCalls(messages) || hasStreamingContent(messages);
};

/**
 * Get a summary of current tool call activity for debugging
 */
export const getToolCallSummary = (messages: Message[]): {
  totalToolCalls: number;
  activeToolCalls: number;
  streamingMessages: number;
} => {
  let totalToolCalls = 0;
  let activeToolCalls = 0;
  let streamingMessages = 0;

  messages.forEach(message => {
    if (message.isStreaming) streamingMessages++;
    
    if (message.toolCalls?.length) {
      totalToolCalls += message.toolCalls.length;
      activeToolCalls += message.toolCalls.filter(tc => 
        tc.status === 'running' || tc.status === 'pending'
      ).length;
    }
  });

  return { totalToolCalls, activeToolCalls, streamingMessages };
};
