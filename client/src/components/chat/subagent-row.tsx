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
import { formatRoleName } from "@/lib/sub-agent-format";

interface SubAgentRowProps {
  toolCall: ToolCall;
  onSelect?: (agentId: string, childSessionId: string) => void;
}

function StatusIcon({ toolCall }: Readonly<{ toolCall: ToolCall }>) {
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

const STRENGTH_TONE_CLASSES: Record<string, string> = {
  strong: "text-emerald-700 dark:text-emerald-400 border-emerald-700/30 dark:border-emerald-400/30",
  moderate: "text-foreground border-input",
  weak: "text-amber-700 dark:text-amber-400 border-amber-700/30 dark:border-amber-400/30",
};

function StrengthChip({ strength }: Readonly<{ strength: NonNullable<ToolCall["self_assessed_strength"]> }>) {
  const tone = STRENGTH_TONE_CLASSES[strength] ?? "text-muted-foreground border-input";
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
  const clickable = !!onSelect;

  const handleSelect = React.useCallback(() => {
    if (!clickable || !parsed) return;
    onSelect?.(parsed.agent_id, parsed.child_session_id);
  }, [clickable, onSelect, parsed]);

  if (!parsed) {
    return (
      <div className="px-3 py-2 text-xs text-muted-foreground">
        Sub-agent dispatch (malformed)
      </div>
    );
  }

  const content = (
    <>
      <StatusIcon toolCall={toolCall} />
      <span className="rounded-sm border border-input bg-muted/40 px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-foreground">
        {formatRoleName(parsed.role_name)}
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
    </>
  );

  if (clickable) {
    return (
      <button
        type="button"
        aria-label={`Open sub-agent ${parsed.role_name}`}
        onClick={handleSelect}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-sm",
          "cursor-pointer transition-colors hover:bg-muted/50 focus:outline-none focus:ring-1 focus:ring-ring",
        )}
      >
        {content}
      </button>
    );
  }

  return (
    <div
      className={cn(
        "flex items-center gap-2 px-3 py-2 text-sm",
        "cursor-default opacity-90",
      )}
    >
      {content}
    </div>
  );
};

export default SubAgentRow;
