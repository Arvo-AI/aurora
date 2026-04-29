'use client';

import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { MarkdownRenderer } from '@/components/ui/markdown-renderer';
import ToolCallWidget from '@/components/tool-calls/ToolCallWidget';
import {
  DataPlanPart,
  DataSubAgentPart,
  MessagePart,
  ReasoningPart,
  TextPart,
  ToolPart,
} from '@/lib/chat-message-parts';
import { ToolCall } from '@/app/chat/types';

interface MessagePartsRendererProps {
  parts: MessagePart[];
  className?: string;
  sessionId?: string;
  userId?: string;
}

// Adapter — AI SDK 5 ToolPart → existing ToolCall shape consumed by ToolCallWidget.
// The legacy widget expects the shape from /app/chat/types#ToolCall (snake_case
// fields, "running" / "completed" / "error" status enum).
function toolPartToLegacyToolCall(part: ToolPart): ToolCall {
  const toolName = part.type.startsWith('tool-') ? part.type.slice('tool-'.length) : part.type;
  let status: ToolCall['status'];
  switch (part.state) {
    case 'input-streaming':
    case 'input-available':
      status = 'running';
      break;
    case 'output-available':
      status = 'completed';
      break;
    case 'output-error':
      status = 'error';
      break;
    default:
      status = 'running';
  }
  const inputStr =
    typeof part.input === 'string' ? part.input : JSON.stringify(part.input ?? '');
  return {
    id: part.toolCallId,
    tool_name: toolName,
    input: inputStr,
    output: part.output,
    error: part.errorText ?? null,
    status,
    timestamp: new Date().toISOString(),
  };
}

function ReasoningBlock({ part }: { part: ReasoningPart }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Reasoning
      </button>
      {open && (
        <div className="mt-1 pl-4 border-l-2 border-zinc-700 italic text-xs text-zinc-400 whitespace-pre-wrap">
          {part.text}
        </div>
      )}
    </div>
  );
}

function SubAgentChip({ part }: { part: DataSubAgentPart }) {
  const { agent_id, ui_label, status, model, summary } = part.data;
  const label = ui_label || agent_id;
  const statusColor =
    status === 'finished'
      ? 'text-emerald-400'
      : status === 'failed'
        ? 'text-red-400'
        : status === 'running' || status === 'dispatched'
          ? 'text-orange-400'
          : 'text-zinc-500';
  const badge =
    status === 'finished' ? '✓' : status === 'failed' ? '✗' : status ? '▷' : '';
  return (
    <div className="my-2 inline-flex items-center gap-2 rounded-md bg-zinc-800/60 border border-zinc-700/60 px-2 py-1 text-xs">
      <span className="text-zinc-200 font-medium">{label}</span>
      {badge && <span className={statusColor}>{badge}</span>}
      {model && <span className="text-zinc-500">{model}</span>}
      {summary && <span className="text-zinc-400 max-w-[28rem] truncate">{summary}</span>}
    </div>
  );
}

function PlanBlock({ part }: { part: DataPlanPart }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-2 rounded-md border border-zinc-700/60 bg-zinc-900/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-1 px-2 py-1.5 text-xs text-zinc-300"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Plan
      </button>
      {open && (
        <div className="px-3 pb-2 text-xs text-zinc-400 space-y-1">
          {part.data.rationale && (
            <div className="whitespace-pre-wrap">{part.data.rationale}</div>
          )}
          {part.data.selected !== undefined && (
            <pre className="text-[11px] bg-zinc-900/60 rounded p-2 overflow-x-auto">
              {JSON.stringify(part.data.selected, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

const MessagePartsRenderer: React.FC<MessagePartsRendererProps> = ({
  parts,
  className,
  sessionId,
  userId,
}) => {
  return (
    <div className={className}>
      {parts.map((part, i) => {
        const key = `p-${i}`;
        // Tool parts are dynamic-typed (`tool-<name>`) so they live outside the
        // switch — keep them as a prefix check.
        if (part.type.startsWith('tool-')) {
          return (
            <div key={key} className="my-2">
              <ToolCallWidget
                tool={toolPartToLegacyToolCall(part as ToolPart)}
                sessionId={sessionId}
                userId={userId}
              />
            </div>
          );
        }
        switch (part.type) {
          case 'text': {
            const text = (part as TextPart).text;
            return text ? <MarkdownRenderer key={key} content={text} /> : null;
          }
          case 'reasoning':
            return <ReasoningBlock key={key} part={part as ReasoningPart} />;
          case 'data-subagent':
            return <SubAgentChip key={key} part={part as DataSubAgentPart} />;
          case 'data-plan':
            return <PlanBlock key={key} part={part as DataPlanPart} />;
          default:
            return null;
        }
      })}
    </div>
  );
};

export default MessagePartsRenderer;
