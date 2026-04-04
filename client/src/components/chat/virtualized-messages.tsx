"use client";

import React, { useRef, useState } from "react";
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
  const [atBottom, setAtBottom] = useState(true);
  const firstLoad = useRef(true);
  const isExistingSession = useRef(false);

  if (firstLoad.current && messages.length > 0) {
    firstLoad.current = false;
    isExistingSession.current = messages.length > 2;
  }

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
      followOutput={(isAtBottom: boolean) => isAtBottom ? "smooth" : false}
      atBottomStateChange={setAtBottom}
      atBottomThreshold={80}
      overscan={600}
      increaseViewportBy={{ top: 400, bottom: 400 }}
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
            allMessages={messages}
            messageIndex={index}
          />
        </div>
      )}
    />
  );
});

VirtualizedMessages.displayName = "VirtualizedMessages";
