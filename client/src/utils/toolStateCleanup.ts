/**
 * Utility to detect and fix "stuck" tool calls in completed chat workflows.
 * 
 * When a workflow completes or crashes, any tool calls still in "running" state
 * are stuck and should be marked as error. This utility detects and fixes them.
 */

import { Message } from "../app/chat/types";

const STALE_TOOL_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Check if a tool call is stuck.
 * 
 * A tool is stuck if EITHER:
 * 1. We're loading a completed chat from DB (workflow done) and it's still "running"
 * 2. OR it's been running for more than 5 minutes (time-based fallback)
 */
function isStuckToolCall(
  toolCallTimestamp: string | undefined,
  sessionUpdatedAt: string | undefined,
  toolStatus: string
): boolean {
  if (toolStatus !== 'running') {
    return false; // Not running, so can't be stuck
  }

  // Condition 1: Loading from DB means workflow is complete
  // If tool is still "running" when we load from DB, it's stuck
  // (Active workflows don't load from DB, they keep tools in memory)
  const isLoadedFromDb = true; // By definition - we're in the load function
  
  // Condition 2: Time-based check as fallback
  const timestampStr = toolCallTimestamp || sessionUpdatedAt;
  let isTooOld = false;
  
  if (timestampStr) {
    const timestamp = new Date(timestampStr).getTime();
    const now = Date.now();
    const age = now - timestamp;
    isTooOld = age > STALE_TOOL_THRESHOLD_MS;
  } else {
    // No timestamp available - assume it's old
    isTooOld = true;
  }

  // OR condition: stuck if loading from DB OR too old
  return isLoadedFromDb || isTooOld;
}

/**
 * Clean up stuck tool calls in loaded messages.
 * Marks tools that are still "running" in completed workflows as error.
 */
export function cleanupStaleToolCalls(
  messages: Message[],
  sessionUpdatedAt?: string
): Message[] {
  const startTime = performance.now();
  let stuckCount = 0;
  let processedCount = 0;

  const cleanedMessages = messages.map(message => {
    // Only process bot messages with tool calls
    if (message.sender !== 'bot' || !message.toolCalls || message.toolCalls.length === 0) {
      return message;
    }

    processedCount++;
    let hasStuckTools = false;
    const cleanedToolCalls = message.toolCalls.map(toolCall => {
      const isStuck = isStuckToolCall(
        toolCall.timestamp,
        sessionUpdatedAt,
        toolCall.status
      );

      if (isStuck) {
        hasStuckTools = true;
        stuckCount++;
        console.warn(
          `Detected stuck tool call: ${toolCall.tool_name} (status: ${toolCall.status}, timestamp: ${toolCall.timestamp || 'unknown'})`
        );

        return {
          ...toolCall,
          status: 'error' as const,
          output: toolCall.output || 'Tool execution was interrupted. The workflow may have crashed before completion.'
        };
      }

      return toolCall;
    });

    if (hasStuckTools) {
      return {
        ...message,
        toolCalls: cleanedToolCalls
      };
    }

    return message;
  });

  const elapsed = performance.now() - startTime;
  if (stuckCount > 0) {
    console.log(`✓ Cleaned up ${stuckCount} stuck tool(s) from ${processedCount} messages in ${elapsed.toFixed(2)}ms`);
  } else if (processedCount > 0) {
    console.log(`✓ Checked ${processedCount} tool messages (${messages.length} total) in ${elapsed.toFixed(2)}ms - no stuck tools found`);
  }

  return cleanedMessages;
}

/**
 * Get a human-readable description of how long ago a timestamp was.
 */
export function getTimeSinceTimestamp(timestamp: string): string {
  const now = Date.now();
  const then = new Date(timestamp).getTime();
  const diffMs = now - then;
  
  const seconds = Math.floor(diffMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  
  if (days > 0) return `${days} day${days > 1 ? 's' : ''} ago`;
  if (hours > 0) return `${hours} hour${hours > 1 ? 's' : ''} ago`;
  if (minutes > 0) return `${minutes} minute${minutes > 1 ? 's' : ''} ago`;
  return `${seconds} second${seconds > 1 ? 's' : ''} ago`;
}

