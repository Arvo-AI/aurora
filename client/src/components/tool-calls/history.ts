import type { ToolCall } from '@/app/chat/types';
import type { ToolCallHistoryEntry } from '@/components/chat/subagent-detail-panel';

const HISTORY_TO_TOOLCALL_STATUS: Record<string, ToolCall['status']> = {
  completed: 'completed',
  success: 'completed',
  succeeded: 'completed',
  running: 'running',
  pending: 'pending',
  error: 'error',
  failed: 'error',
  cancelled: 'cancelled',
};

function safeStringify(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return String(value);
  }
}

export function historyEntryId(entry: ToolCallHistoryEntry, idx: number): string {
  return `${entry.tool_name}-${entry.started_at ?? idx}`;
}

export function historyEntryToToolCall(
  entry: ToolCallHistoryEntry,
  id: string,
  isExpanded: boolean,
): ToolCall {
  // `entry.command` is captured server-side before the args blob is truncated,
  // so the widget can show the actual command even when args/output are clipped.
  const entryCommand = (entry as { command?: string }).command;
  return {
    id,
    tool_name: entry.tool_name,
    input: safeStringify(entry.args),
    output: entry.output_excerpt ?? '',
    status: HISTORY_TO_TOOLCALL_STATUS[entry.status] ?? 'completed',
    timestamp: entry.started_at ?? '',
    isExpanded,
    ...(entryCommand ? { command: entryCommand } : {}),
  };
}
