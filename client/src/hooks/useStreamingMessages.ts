"use client";

import { useState, useRef, useCallback } from "react";
import { Message } from "../app/chat/types";

export interface StreamingMessageState {
  currentStreamingMessage: Message | null;
  startStreamingMessage: (isThinking?: boolean) => void;
  appendToStreamingMessage: (chunk: string) => void;
  finishStreamingMessage: () => Message | null;
  isStreaming: boolean;
  checkIsStreaming: () => boolean;
  cleanup: () => void;
}

export const useStreamingMessages = (): StreamingMessageState => {
  const [currentStreamingMessage, setCurrentStreamingMessage] = useState<Message | null>(null);
  const streamingBufferRef = useRef<string>("");
  const streamingMessageIdRef = useRef<number | null>(null);
  const streamingTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const isThinkingRef = useRef<boolean>(false);
  const rafRef = useRef<number | null>(null);
  const pendingFlushRef = useRef(false);

  const startStreamingMessage = useCallback((isThinking: boolean = false) => {
    const messageId = Date.now();
    streamingMessageIdRef.current = messageId;
    streamingBufferRef.current = "";
    isThinkingRef.current = isThinking;
    
    const newStreamingMessage: Message = {
      id: messageId,
      sender: "bot",
      text: "",
      isStreaming: true,
      isThinking: isThinking
    };
    
    setCurrentStreamingMessage(newStreamingMessage);
  }, []);

  const appendToStreamingMessage = useCallback((chunk: string) => {
    if (streamingMessageIdRef.current) {
      streamingBufferRef.current += chunk;
      
      if (!pendingFlushRef.current) {
        pendingFlushRef.current = true;
        rafRef.current = requestAnimationFrame(() => {
          pendingFlushRef.current = false;
          const text = streamingBufferRef.current;
          setCurrentStreamingMessage(prev => prev ? {
            ...prev,
            text
          } : null);
        });
      }
    }
  }, []);

  const finishStreamingMessage = useCallback(() => {
    if (streamingMessageIdRef.current && streamingBufferRef.current) {
      if (streamingTimeoutRef.current) {
        clearTimeout(streamingTimeoutRef.current);
        streamingTimeoutRef.current = null;
      }
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
        pendingFlushRef.current = false;
      }
      
      const finalMessage: Message = {
        id: streamingMessageIdRef.current,
        sender: "bot",
        text: streamingBufferRef.current,
        isStreaming: false,
        isThinking: isThinkingRef.current
      };

      setCurrentStreamingMessage(null);
      streamingMessageIdRef.current = null;
      streamingBufferRef.current = "";
      isThinkingRef.current = false;
      
      return finalMessage;
    }
    return null;
  }, []);

  // Cleanup timeout on unmount
  const cleanup = useCallback(() => {
    if (streamingTimeoutRef.current) {
      clearTimeout(streamingTimeoutRef.current);
    }
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
    }
  }, []);

  // Create a function to check streaming status that reads the ref directly
  const checkIsStreaming = useCallback(() => {
    return !!streamingMessageIdRef.current;
  }, []);

  return {
    currentStreamingMessage,
    startStreamingMessage,
    appendToStreamingMessage,
    finishStreamingMessage,
    isStreaming: !!streamingMessageIdRef.current,
    checkIsStreaming, // Add this function for real-time checks
    cleanup
  };
};
