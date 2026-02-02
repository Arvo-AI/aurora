"use client";

import React, { useState } from "react";
import { Message } from "../../app/chat/types";
import { MarkdownRenderer } from "../ui/markdown-renderer";
import { Copy, Check } from "lucide-react";
import { Button } from "../ui/button";

// Import the tool call widget router (routes to custom widgets)
import ToolCallWidget from "../tool-calls/ToolCallWidget";

interface MessageItemProps {
  message: Message;
  sendRaw?: (data: string) => boolean;
  onUpdateMessage?: (messageId: number, updater: (message: Message) => Message) => void;
  sessionId?: string;
  userId?: string;
  allMessages?: Message[];
  messageIndex?: number;
}

export const MessageItem = React.memo(({ message, sendRaw, onUpdateMessage, sessionId, userId, allMessages, messageIndex }: MessageItemProps) => {
  const [copied, setCopied] = useState(false);

  // Simple check: is this the last bot message before a user message?
  const isLastBotMessage = React.useMemo(() => {
    if (message.sender !== "bot" || !allMessages || messageIndex === undefined) {
      return false;
    }
    const nextMessage = allMessages[messageIndex + 1];
    return !nextMessage || nextMessage.sender === "user";
  }, [message.sender, allMessages, messageIndex]);

  const sortedToolCalls = React.useMemo(() => {
    if (!message.toolCalls?.length) {
      return [];
    }
    return [...message.toolCalls].sort((a, b) => {
      const aTime = a.timestamp ? new Date(a.timestamp).getTime() : 0;
      const bTime = b.timestamp ? new Date(b.timestamp).getTime() : 0;
      return aTime - bTime;
    });
  }, [message.toolCalls]);

  const handleCopy = async () => {
    try {
      if (!allMessages || messageIndex === undefined) {
        // Fallback: just copy this message
        await navigator.clipboard.writeText(message.text || "");
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
        return;
      }

      // Find start of bot message group (go backwards until we hit a user message)
      let startIndex = messageIndex;
      while (startIndex > 0 && allMessages[startIndex - 1].sender === "bot") {
        startIndex--;
      }
      
      // Collect all text and tool calls from consecutive bot messages
      let textToCopy = "";
      for (let i = startIndex; i <= messageIndex; i++) {
        const msg = allMessages[i];
        if (msg.sender !== "bot") continue;
        
        // Add text content
        if (msg.text && msg.text.trim().length > 0) {
          if (textToCopy.length > 0) textToCopy += "\n\n";
          textToCopy += msg.text;
        }
        
        // Add tool call information
        if (msg.toolCalls && msg.toolCalls.length > 0) {
          const toolCallsForCopy = [...msg.toolCalls].sort((a, b) => {
            const aTime = a.timestamp ? new Date(a.timestamp).getTime() : 0;
            const bTime = b.timestamp ? new Date(b.timestamp).getTime() : 0;
            return aTime - bTime;
          });
          toolCallsForCopy.forEach(tc => {
            textToCopy += `\n\n--- Tool Call: ${tc.tool_name} ---\n`;
            textToCopy += `Input: ${tc.input}\n`;
            if (tc.output) {
              textToCopy += `Output: ${typeof tc.output === 'string' ? tc.output : JSON.stringify(tc.output, null, 2)}\n`;
            }
            if (tc.error) {
              textToCopy += `Error: ${tc.error}\n`;
            }
            textToCopy += `Status: ${tc.status}`;
          });
        }
      }
      
      await navigator.clipboard.writeText(textToCopy);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy message:', err);
    }
  };

  return (
  <div className={`flex ${message.sender === "user" ? "justify-end" : "justify-start"} px-4 py-1`}>
    <div
      className={
        message.sender === "user"
          ? "rounded-2xl p-4 max-w-[80%] bg-muted text-foreground"
          : "w-full text-foreground"
      }
    >
      <div className="break-words leading-relaxed">
        <MarkdownRenderer content={message.text || ""} />
        {message.isStreaming && (
          <span className="inline-block w-2 h-5 bg-current animate-pulse ml-1 opacity-75">|</span>
        )}
      </div>

      {/* Display images for user messages */}
      {message.images && message.images.length > 0 && (
        <div className="flex flex-wrap gap-2 mt-2">
          {message.images.map((img, idx) => (
            <img
              key={idx}
              src={img.displayData || `data:${img.type};base64,${img.data}`}
              alt={img.name || `Image ${idx + 1}`}
              className="max-w-xs rounded-lg border border-input"
            />
          ))}
        </div>
      )}
      
      {/* Tool Calls - routed through ToolCallWidget for custom widgets */}
      {!!sortedToolCalls.length && (
        <div className="mt-3 space-y-2">
          {sortedToolCalls
            .filter(toolCall => toolCall.tool_name !== 'unknown' || (toolCall.input && toolCall.input !== '{}' && JSON.stringify(toolCall.input) !== '{}'))
            .map((toolCall, index) => (
            <ToolCallWidget 
              key={toolCall.id || `tool-${index}`}
              tool={toolCall}
              sendRaw={sendRaw}
              sessionId={sessionId}
              userId={userId}
              onToolUpdate={(updates) => {
                // Update this specific tool call in the message
                onUpdateMessage?.(message.id, (msg) => ({
                  ...msg,
                  toolCalls: msg.toolCalls?.map(tc => 
                    tc.id === toolCall.id ? { ...tc, ...updates } : tc
                  )
                }));
              }}
            />
          ))}
        </div>
      )}

      {/* Copy button - only on the last bot message in a group */}
      {message.sender === "bot" && !message.isStreaming && isLastBotMessage && (
        <div className="flex justify-end mt-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleCopy}
            className="h-6 w-6 p-0 hover:bg-muted"
          >
            {copied ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
        </div>
      )}
    </div>
  </div>
  );
});
