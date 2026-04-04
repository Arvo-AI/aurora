"use client";

import React, { useRef, useEffect, useState } from "react";
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

export const VirtualizedMessages = React.memo(({ messages, sendRaw, onUpdateMessage, sessionId, userId }: VirtualizedMessagesProps) => {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const prevCount = useRef(0);
  const [atBottom, setAtBottom] = useState(false);
  const isExistingSession = useRef(false);
  const hasInitialized = useRef(false);
  const [virtuosoKey, setVirtuosoKey] = useState(0);

  // Detect session type synchronously during render (before Virtuoso mounts)
  // so initialTopMostItemIndex and followOutput get the correct value on first mount.
  if (!hasInitialized.current && messages.length > 0) {
    hasInitialized.current = true;
    isExistingSession.current = messages.length > 2;
  }

  useEffect(() => {
    const prev = prevCount.current;
    const cur = messages.length;

    if (cur > prev && prev > 0 && (!isExistingSession.current || atBottom)) {
      virtuosoRef.current?.scrollToIndex({
        index: cur - 1,
        align: "end",
        behavior: "smooth",
      });
    }

    prevCount.current = cur;
  }, [messages.length, atBottom]);

  useEffect(() => {
    prevCount.current = 0;
    hasInitialized.current = false;
    isExistingSession.current = false;
    setVirtuosoKey(k => k + 1);
  }, [sessionId]);

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
      key={virtuosoKey}
      ref={virtuosoRef}
      data={messages}
      totalCount={messages.length}
      initialTopMostItemIndex={isExistingSession.current ? 0 : messages.length - 1}
      followOutput={(isAtBottom: boolean) => {
        if (isExistingSession.current) return false;
        return isAtBottom ? "smooth" : false;
      }}
      atBottomStateChange={setAtBottom}
      atBottomThreshold={80}
      overscan={600}
      increaseViewportBy={{ top: 400, bottom: 400 }}
      className="h-full scrollbar-thin scrollbar-track-transparent scrollbar-thumb-gray-300 dark:scrollbar-thumb-gray-600"
      components={{
        Footer: () => <div className="h-8" />,
      }}
      itemContent={(index: number, message: Message) => (
        <MessageItem
          message={message}
          sendRaw={sendRaw}
          onUpdateMessage={onUpdateMessage}
          sessionId={sessionId}
          userId={userId}
          allMessages={messages}
          messageIndex={index}
        />
      )}
    />
  );
});

VirtualizedMessages.displayName = "VirtualizedMessages";
