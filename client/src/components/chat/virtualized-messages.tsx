"use client";

import React, { useRef, useEffect, useMemo, useCallback, useLayoutEffect, useState } from "react";
import { VariableSizeList as List } from 'react-window';
import { MessageItem } from "./message-item";
import { Message } from "../../app/chat/types";

interface VirtualizedMessagesProps {
  messages: Message[];
  sendRaw?: (data: string) => boolean;
  onUpdateMessage?: (messageId: number, updater: (message: Message) => Message) => void;
  sessionId?: string;
  userId?: string;
}

// Individual message row component for react-window
const MessageRow = React.memo(({ index, style, data }: {
  index: number;
  style: React.CSSProperties;
  data: { 
    messages: Message[]; 
    getItemSize: (index: number, height?: number) => void;
    sendRaw?: (data: string) => boolean;
    onUpdateMessage?: (messageId: number, updater: (message: Message) => Message) => void;
    sessionId?: string;
    userId?: string;
  };
}) => {
  const message = data.messages[index];
  const measureRef = useRef<HTMLDivElement>(null);

  // Measure immediately after layout and respond to live size changes
  useLayoutEffect(() => {
    const node = measureRef.current;
    if (!node) return;

    const measure = () => {
      const height = Math.ceil(node.getBoundingClientRect().height);
      data.getItemSize(index, height);
    };

    // Initial synchronous measure
    measure();

    // Observe growth/shrink (tool output expand/collapse, Monaco mount, etc.)
    let rafId: number | null = null;
    const ro = new ResizeObserver(() => {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(measure);
    });
    ro.observe(node);

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
    };
  }, [index, data]);

  return (
    <div style={style}>
      <div ref={measureRef}>
        <MessageItem 
          message={message} 
          sendRaw={data.sendRaw}
          onUpdateMessage={data.onUpdateMessage}
          sessionId={data.sessionId}
          userId={data.userId}
          allMessages={data.messages}
          messageIndex={index}
        />
      </div>
    </div>
  );
});

MessageRow.displayName = "MessageRow";

export const VirtualizedMessages = React.memo(({ messages, sendRaw, onUpdateMessage, sessionId, userId }: VirtualizedMessagesProps) => {
  const listRef = useRef<List>(null);
  const shouldAutoScroll = useRef(true);
  const itemSizes = useRef<Map<number, number>>(new Map());
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const resizeObserver = new ResizeObserver(() => {
      setSize({
        width: element.offsetWidth,
        height: element.offsetHeight
      });
    });

    resizeObserver.observe(element);
    
    // Initial measure
    setSize({
      width: element.offsetWidth,
      height: element.offsetHeight
    });

    return () => resizeObserver.disconnect();
  }, []);

  // Default estimated height for messages - will be updated with actual measurements  
  const ESTIMATED_ITEM_HEIGHT = 100;

  // Get item size with dynamic measurement
  const getItemSize = useCallback((index: number, measuredHeight?: number) => {
    if (measuredHeight !== undefined) {
      const oldHeight = itemSizes.current.get(index);
      
      // Only update if height actually changed
      if (oldHeight !== measuredHeight) {
        itemSizes.current.set(index, measuredHeight);
        
          // Force list to re-render immediately
          if (listRef.current) {
            // Reset all items from this index onward
            listRef.current.resetAfterIndex(index, true);
            
            // Force update to ensure the list re-renders with new sizes
            // This is necessary when items change size dynamically
            requestAnimationFrame(() => {
              if (listRef.current) {
                // Trigger a re-render by scrolling to the current position
                const scrollOffset = (listRef.current as any).state?.scrollOffset || 0;
                listRef.current.scrollTo(scrollOffset);
              }
            });
          }
      }
    }
    return itemSizes.current.get(index) || ESTIMATED_ITEM_HEIGHT;
  }, [messages.length]);

  // Data passed to each row component
  const itemData = useMemo(() => ({
    messages,
    getItemSize: (index: number, height?: number) => getItemSize(index, height),
    sendRaw,
    onUpdateMessage,
    sessionId,
    userId
  }), [messages, getItemSize, sendRaw, onUpdateMessage, sessionId, userId]);

  // Auto-scroll to bottom for new messages
  useEffect(() => {
    if (listRef.current && messages.length > 0 && shouldAutoScroll.current) {
      // Small delay to ensure the list has rendered the new items
      requestAnimationFrame(() => {
        if (listRef.current) {
          listRef.current.scrollToItem(messages.length - 1, "end");
        }
      });
    }
  }, [messages.length]);

  // Handle scroll events to determine if we should auto-scroll
  const handleScroll = useCallback(({ scrollOffset, scrollUpdateWasRequested }: {
    scrollOffset: number;
    scrollUpdateWasRequested: boolean;
  }) => {
    if (!scrollUpdateWasRequested && containerRef.current) {
      const container = containerRef.current;
      const { scrollHeight, clientHeight } = container;
      // If user scrolled up from bottom, disable auto-scroll
      shouldAutoScroll.current = scrollOffset + clientHeight >= scrollHeight - 100;
    }
  }, []);

  // Clear item size cache when messages change significantly
  useEffect(() => {
    itemSizes.current.clear();
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
    <div ref={containerRef} className="w-full h-full pb-4">
      <List
        ref={listRef}
        height={size.height}
        width={size.width}
        itemCount={messages.length}
        itemSize={getItemSize}
        itemData={itemData}
        onScroll={handleScroll}
        className="scrollbar-thin scrollbar-track-transparent scrollbar-thumb-gray-300 dark:scrollbar-thumb-gray-600"
        overscanCount={5} // Render 5 extra items outside viewport for smoother scrolling
      >
        {MessageRow}
      </List>
    </div>
  );
});

VirtualizedMessages.displayName = "VirtualizedMessages";