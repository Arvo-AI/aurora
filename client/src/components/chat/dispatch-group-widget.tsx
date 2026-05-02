"use client";

import * as React from "react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Loader2,
} from "lucide-react";
import { useTheme } from "next-themes";
import { ToolCall } from "@/app/chat/types";
import SubAgentRow from "./subagent-row";

interface DispatchGroupWidgetProps {
  toolCalls: ToolCall[];
  incidentId?: string;
  onSelectSubAgent?: (agentId: string, childSessionId: string) => void;
  className?: string;
}

type Aggregate = "running" | "warning" | "succeeded";

function aggregateStatus(toolCalls: ToolCall[]): Aggregate {
  let anyRunning = false;
  let anyBad = false;
  for (const tc of toolCalls) {
    if (tc.status === "running" || tc.status === "pending") {
      anyRunning = true;
    }
    if (tc.status === "error" || tc.status === "cancelled") {
      anyBad = true;
    }
    const outStatus =
      tc.output && typeof tc.output === "object"
        ? (tc.output as { status?: string }).status
        : undefined;
    if (outStatus === "failed" || outStatus === "timeout" || outStatus === "cancelled") {
      anyBad = true;
    }
  }
  if (anyRunning) return "running";
  if (anyBad) return "warning";
  return "succeeded";
}

function AggregateIcon({ status }: { status: Aggregate }) {
  if (status === "running") {
    return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />;
  }
  if (status === "warning") {
    return <AlertCircle className="h-4 w-4 text-amber-500 dark:text-amber-400" />;
  }
  return <CheckCircle2 className="h-4 w-4 text-emerald-500 dark:text-emerald-400" />;
}

const DispatchGroupWidget = ({
  toolCalls,
  incidentId,
  onSelectSubAgent,
  className,
}: DispatchGroupWidgetProps) => {
  const { theme } = useTheme();
  const [expanded, setExpanded] = React.useState(false);
  const status = React.useMemo(() => aggregateStatus(toolCalls), [toolCalls]);
  const count = toolCalls.length;
  const canNavigate = !!incidentId && !!onSelectSubAgent;

  const toggle = React.useCallback(() => {
    setExpanded((v) => !v);
  }, []);

  return (
    <Card
      className={cn(
        "w-full overflow-hidden border border-border font-mono text-sm",
        className,
      )}
      style={{ backgroundColor: theme === "dark" ? "#000000" : "white" }}
    >
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
        className="flex cursor-pointer items-center justify-between px-4 py-3 transition-colors hover:bg-muted/40"
      >
        <div className="flex min-w-0 items-center gap-2 text-foreground">
          <AggregateIcon status={status} />
          <span className="text-sm">
            Ran {count} agent{count === 1 ? "" : "s"}
          </span>
          {!canNavigate && (
            <span className="ml-2 text-xs text-muted-foreground">
              Open an incident to view details
            </span>
          )}
        </div>
        <button
          type="button"
          aria-label={expanded ? "Collapse agents" : "Expand agents"}
          onClick={(e) => {
            e.stopPropagation();
            toggle();
          }}
          className="flex flex-shrink-0 items-center justify-center text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? (
            <ChevronUp className="h-4 w-4" />
          ) : (
            <ChevronDown className="h-4 w-4" />
          )}
        </button>
      </div>

      {expanded && (
        <div className="border-t border-border">
          {toolCalls.map((tc, idx) => (
            <div
              key={tc.id || `dispatch-${idx}`}
              className={cn(idx > 0 && "border-t border-border")}
            >
              <SubAgentRow
                toolCall={tc}
                onSelect={canNavigate ? onSelectSubAgent : undefined}
              />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
};

export default DispatchGroupWidget;
