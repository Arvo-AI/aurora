"use client";

import React, { useRef, useEffect } from "react";
import { MessageItem } from "./message-item";
import { Message } from "../../app/chat/types";

interface MessageListProps {
  messages: Message[];
  sendRaw?: (data: string) => boolean;
  onUpdateMessage?: (messageId: number, updater: (message: Message) => Message) => void;
  sessionId?: string;
  userId?: string;
  incidentId?: string;
  onSelectSubAgent?: (agentId: string, childSessionId: string) => void;
}

export function MessageList({ messages, sendRaw, onUpdateMessage, sessionId, userId, incidentId, onSelectSubAgent }: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);
  const prevMessageCountRef = useRef(messages.length);
  // Stays true after user sends a message until they manually scroll up
  const stickyScrollRef = useRef(true);

  // Track whether user is near the bottom
  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const threshold = 80;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    isAtBottomRef.current = atBottom;
    // Only clear sticky scroll when user deliberately scrolls up
    if (!atBottom) {
      stickyScrollRef.current = false;
    }
  };

  // Scroll to bottom on initial mount / session switch
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "instant" });
  }, []);

  // Auto-scroll: on send, on new messages, and during streaming
  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;

    const lastMessage = messages[messages.length - 1];
    const shouldScroll = isAtBottomRef.current || stickyScrollRef.current;

    // Always scroll when the user sends a message (even if scrolled up)
    if (messages.length > prevCount && lastMessage?.sender === "user") {
      bottomRef.current?.scrollIntoView({ behavior: "instant" });
      isAtBottomRef.current = true;
      stickyScrollRef.current = true;
      return;
    }

    // Scroll on new messages if at bottom or sticky
    if (messages.length > prevCount && shouldScroll) {
      bottomRef.current?.scrollIntoView({ behavior: "instant" });
      return;
    }

    // Keep scrolling during streaming if at bottom or sticky
    if (lastMessage?.isStreaming && shouldScroll) {
      bottomRef.current?.scrollIntoView({ behavior: "instant" });
    }
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-xl font-medium mb-2">Welcome to Aurora</h2>
          <p className="text-muted-foreground">Start a conversation to get started</p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="h-full overflow-y-auto scrollbar-thin scrollbar-track-transparent scrollbar-thumb-gray-300 dark:scrollbar-thumb-gray-600"
    >
      <div className="h-6" />
      {messages.map((message, index) => (
        <div key={`${message.id}-${index}`} className="max-w-4xl mx-auto px-4">
          <MessageItem
            message={message}
            sendRaw={sendRaw}
            onUpdateMessage={onUpdateMessage}
            sessionId={sessionId}
            userId={userId}
            allMessages={messages}
            messageIndex={index}
            incidentId={incidentId}
            onSelectSubAgent={onSelectSubAgent}
          />
        </div>
      ))}
      <div className="h-8" ref={bottomRef} />
    </div>
  );
}
