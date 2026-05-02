"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Loader2,
  XCircle,
} from "lucide-react";
import { ToolCall, parseDispatchToolCall } from "@/app/chat/types";

interface SubAgentRowProps {
  toolCall: ToolCall;
  onSelect?: (agentId: string, childSessionId: string) => void;
}

function StatusIcon({ toolCall }: { toolCall: ToolCall }) {
  if (toolCall.status === "running" || toolCall.status === "pending") {
    return <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />;
  }
  if (toolCall.status === "error" || toolCall.status === "cancelled") {
    return <XCircle className="h-3.5 w-3.5 text-red-500 dark:text-red-400" />;
  }
  const outStatus =
    toolCall.output && typeof toolCall.output === "object"
      ? (toolCall.output as { status?: string }).status
      : undefined;
  if (outStatus === "failed" || outStatus === "timeout" || outStatus === "cancelled") {
    return <AlertCircle className="h-3.5 w-3.5 text-amber-500 dark:text-amber-400" />;
  }
  return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 dark:text-emerald-400" />;
}

function StrengthChip({ strength }: { strength: NonNullable<ToolCall["self_assessed_strength"]> }) {
  const tone =
    strength === "strong"
      ? "text-emerald-700 dark:text-emerald-400 border-emerald-700/30 dark:border-emerald-400/30"
      : strength === "moderate"
        ? "text-foreground border-input"
        : strength === "weak"
          ? "text-amber-700 dark:text-amber-400 border-amber-700/30 dark:border-amber-400/30"
          : "text-muted-foreground border-input";
  return (
    <span
      className={cn(
        "rounded-sm border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        tone,
      )}
    >
      {strength}
    </span>
  );
}

const SubAgentRow = ({ toolCall, onSelect }: SubAgentRowProps) => {
  const parsed = parseDispatchToolCall(toolCall);

  if (!parsed) {
    return (
      <div className="px-3 py-2 text-xs text-muted-foreground">
        Sub-agent dispatch (malformed)
      </div>
    );
  }

  const clickable = !!onSelect;

  const handleSelect = React.useCallback(() => {
    if (!clickable) return;
    onSelect?.(parsed.agent_id, parsed.child_session_id);
  }, [clickable, onSelect, parsed.agent_id, parsed.child_session_id]);

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (!clickable) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handleSelect();
      }
    },
    [clickable, handleSelect],
  );

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open sub-agent ${parsed.role_name}`}
      onClick={handleSelect}
      onKeyDown={handleKeyDown}
      className={cn(
        "flex items-center gap-2 px-3 py-2 text-sm transition-colors focus:outline-none focus:ring-1 focus:ring-ring",
        clickable
          ? "cursor-pointer hover:bg-muted/50"
          : "cursor-default opacity-90",
      )}
    >
      <StatusIcon toolCall={toolCall} />
      <span className="rounded-sm border border-input bg-muted/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
        {parsed.role_name}
      </span>
      <span className="flex-1 truncate text-foreground" title={parsed.purpose}>
        {parsed.purpose}
      </span>
      {parsed.self_assessed_strength && (
        <StrengthChip strength={parsed.self_assessed_strength} />
      )}
      {clickable && (
        <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
      )}
    </div>
  );
};

export default SubAgentRow;
