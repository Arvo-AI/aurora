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
}

export function MessageList({ messages, sendRaw, onUpdateMessage, sessionId, userId }: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);
  const prevMessageCountRef = useRef(messages.length);

  // Track whether user is near the bottom
  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const threshold = 80;
    isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  };

  // Auto-scroll to bottom when new messages arrive (only if already at bottom)
  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;

    if (messages.length > prevCount && isAtBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
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
          />
        </div>
      ))}
      <div className="h-8" ref={bottomRef} />
    </div>
  );
}
