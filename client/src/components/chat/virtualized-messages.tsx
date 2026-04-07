"use client";

import React, { useRef, useEffect, useCallback } from "react";
import { Virtuoso, VirtuosoHandle } from "react-virtuoso";
import { MessageItem } from "./message-item";
import { Message } from "../../app/chat/types";

interface VirtualizedMessagesProps {
  messages: Message[];
  sendRaw?: (data: string) => boolean;
  onUpdateMessage?: (messageId: number, updater: (message: Message) => Message) => void;
  sessionId?: string;
  userId?: string;
}

export function VirtualizedMessages({ messages, sendRaw, onUpdateMessage, sessionId, userId }: VirtualizedMessagesProps) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const isExistingSession = useRef(false);
  const firstLoad = useRef(true);
  const prevMessageCountRef = useRef(messages.length);

  if (firstLoad.current && messages.length > 0) {
    firstLoad.current = false;
    isExistingSession.current = messages.length > 2;
  }

  const handleAtBottomChange = useCallback((_bottom: boolean) => {}, []);

  const handleFollowOutput = useCallback(
    (atBottom: boolean) => (atBottom ? "auto" : false),
    []
  );

  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;

    if (messages.length > prevCount && messages.length > 0) {
      const lastMsg = messages[messages.length - 1];
      if (lastMsg.sender === "user") {
        requestAnimationFrame(() => {
          virtuosoRef.current?.scrollToIndex({
            index: messages.length - 1,
            behavior: "smooth",
            align: "end",
          });
        });
      }
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
    <Virtuoso
      ref={virtuosoRef}
      data={messages}
      totalCount={messages.length}
      initialTopMostItemIndex={isExistingSession.current ? 0 : messages.length - 1}
      followOutput={handleFollowOutput}
      atBottomStateChange={handleAtBottomChange}
      atBottomThreshold={60}
      overscan={400}
      increaseViewportBy={{ top: 400, bottom: 200 }}
      className="h-full scrollbar-thin scrollbar-track-transparent scrollbar-thumb-gray-300 dark:scrollbar-thumb-gray-600"
      components={{
        Header: () => <div className="h-6" />,
        Footer: () => <div className="h-8" />,
      }}
      itemContent={(index: number, message: Message) => (
        <div className="max-w-4xl mx-auto px-4">
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
      )}
    />
  );
}
