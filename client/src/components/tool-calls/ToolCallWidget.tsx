"use client"

import * as React from "react"
import { ToolCall } from "@/app/chat/types";
import ToolExecutionWidget from "./ToolExecutionWidget"

interface ToolCallWidgetProps {
  tool: ToolCall
  className?: string
  sendMessage?: (query: string, userId: string, additionalData?: any) => boolean
  sendRaw?: (data: string) => boolean
  onToolUpdate?: (updatedTool: Partial<ToolCall>) => void
  sessionId?: string
  userId?: string
}

const ToolCallWidget = ({ tool, className, sendMessage, sendRaw, onToolUpdate, sessionId, userId }: ToolCallWidgetProps) => {
  // Delegate all other tools to the generic ToolExecutionWidget for a unified look & feel
  return (
    <ToolExecutionWidget 
      tool={tool as any} 
      className={className} 
      sendMessage={sendMessage}
      sendRaw={sendRaw}
      onToolUpdate={onToolUpdate}
      sessionId={sessionId}
      userId={userId}
    />
  )
}

export default ToolCallWidget; 