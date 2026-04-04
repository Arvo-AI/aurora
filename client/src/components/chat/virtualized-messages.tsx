"use client";

import React, { useRef, useState, useEffect, useCallback, useMemo } from "react";
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
  const firstLoad = useRef(true);
  const isExistingSession = useRef(false);
  const prevMessageCountRef = useRef(messages.length);
  const [userScrolledUp, setUserScrolledUp] = useState(false);
  const frozenMessagesRef = useRef<Message[]>(messages);

  if (firstLoad.current && messages.length > 0) {
    firstLoad.current = false;
    isExistingSession.current = messages.length > 2;
  }

  if (!userScrolledUp) {
    frozenMessagesRef.current = messages;
  } else if (messages.length !== frozenMessagesRef.current.length) {
    frozenMessagesRef.current = messages;
  }

  const stableMessages = userScrolledUp ? frozenMessagesRef.current : messages;

  const handleAtBottomChange = useCallback((bottom: boolean) => {
    if (bottom) {
      setUserScrolledUp(false);
    }
  }, []);

  const handleFollowOutput = useCallback((isAtBottom: boolean) => {
    if (userScrolledUp) return false;
    return isAtBottom ? "smooth" : false;
  }, [userScrolledUp]);

  const handleScroll = useCallback((e: React.UIEvent) => {
    const el = e.target as HTMLElement;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distFromBottom > 150) {
      if (!userScrolledUp) setUserScrolledUp(true);
    } else if (distFromBottom < 80) {
      if (userScrolledUp) setUserScrolledUp(false);
    }
  }, [userScrolledUp]);

  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;

    if (messages.length > prevCount && messages.length > 0) {
      const lastMsg = messages[messages.length - 1];
      if (lastMsg.sender === "user") {
        setUserScrolledUp(false);
        requestAnimationFrame(() => {
          virtuosoRef.current?.scrollToIndex({
            index: messages.length - 1,
            behavior: "smooth",
            align: "end",
          });
        });
      }
    }
  }, [messages.length]);

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
      data={stableMessages}
      totalCount={stableMessages.length}
      initialTopMostItemIndex={isExistingSession.current ? 0 : messages.length - 1}
      followOutput={handleFollowOutput}
      atBottomStateChange={handleAtBottomChange}
      atBottomThreshold={40}
      overscan={200}
      increaseViewportBy={{ top: 200, bottom: 0 }}
      onScroll={handleScroll}
      className="h-full scrollbar-thin scrollbar-track-transparent scrollbar-thumb-gray-300 dark:scrollbar-thumb-gray-600"
      components={{
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
            allMessages={stableMessages}
            messageIndex={index}
          />
        </div>
      )}
    />
  );
});

VirtualizedMessages.displayName = "VirtualizedMessages";
