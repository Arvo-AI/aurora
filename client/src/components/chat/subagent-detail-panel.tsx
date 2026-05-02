"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { AlertCircle, CheckCircle2, Loader2, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { MarkdownRenderer } from "@/components/ui/markdown-renderer";

interface SubAgentDetailPanelProps {
  incidentId: string;
  agentId: string;
  roleName?: string;
  purpose?: string;
  childSessionId?: string;
  onClose: () => void;
  className?: string;
}

export interface ToolCallHistoryEntry {
  tool_name: string;
  args?: unknown;
  output_excerpt?: string;
  status: string;
  started_at?: string;
  completed_at?: string;
}

interface FindingPayload {
  agent_id: string;
  body: string;
  status?: string;
  role_name?: string;
  time_window?: string;
  tool_call_history?: ToolCallHistoryEntry[];
}

const TERMINAL_STATUSES = new Set([
  "succeeded",
  "failed",
  "timeout",
  "cancelled",
  "inconclusive",
]);

function abbreviateValue(value: unknown, max = 200): string {
  if (value === null || value === undefined) return "";
  let s: string;
  if (typeof value === "string") {
    s = value;
  } else {
    try {
      s = JSON.stringify(value);
    } catch {
      s = String(value);
    }
  }
  return s.length > max ? `${s.slice(0, max)}...` : s;
}

function ToolCallStatusIcon({ status }: { status: string }) {
  if (status === "running" || status === "pending") {
    return <Loader2 className="h-3.5 w-3.5 flex-shrink-0 animate-spin text-muted-foreground" />;
  }
  if (status === "error" || status === "failed" || status === "cancelled") {
    return <XCircle className="h-3.5 w-3.5 flex-shrink-0 text-destructive" />;
  }
  return <CheckCircle2 className="h-3.5 w-3.5 flex-shrink-0 text-emerald-500" />;
}

const POLL_INTERVAL_MS = 5000;

const SubAgentDetailPanel = ({
  incidentId,
  agentId,
  roleName,
  purpose,
  onClose,
  className,
}: SubAgentDetailPanelProps) => {
  const [finding, setFinding] = React.useState<FindingPayload | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const fetchFinding = async (isInitial: boolean) => {
      if (isInitial) setLoading(true);
      try {
        const res = await fetch(
          `/api/incidents/${incidentId}/findings/${agentId}`,
          { method: "GET", cache: "no-store", credentials: "include" },
        );
        if (cancelled) return;
        if (!res.ok) {
          // 404 is expected while running and findings don't exist yet
          if (res.status === 404 && isInitial) {
            setFinding(null);
            setError(null);
            return;
          }
          throw new Error(`Request failed (${res.status})`);
        }
        const data = (await res.json()) as FindingPayload;
        if (cancelled) return;
        setFinding(data);
        setError(null);
        // Stop polling once we hit a terminal status
        if (data.status && TERMINAL_STATUSES.has(data.status) && intervalId) {
          clearInterval(intervalId);
          intervalId = null;
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled && isInitial) setLoading(false);
      }
    };

    fetchFinding(true);
    intervalId = setInterval(() => fetchFinding(false), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [incidentId, agentId, reloadKey]);

  const displayRole = finding?.role_name || roleName || agentId;
  const displayPurpose = purpose || "";
  const timeWindow = finding?.time_window;

  return (
    <div
      className={cn(
        "flex max-h-[60vh] w-full flex-col overflow-hidden rounded-md border border-border bg-background",
        className,
      )}
      role="complementary"
      aria-label="Sub-agent details"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">
            {displayRole}
          </div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label="Close sub-agent panel"
          className="h-7 w-7 p-0"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* Brief */}
        <section className="border-b border-border px-4 py-3">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Brief
          </h3>
          <div className="text-sm font-medium text-foreground">{displayRole}</div>
          {displayPurpose && (
            <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
              {displayPurpose}
            </p>
          )}
          {timeWindow && (
            <p className="mt-2 text-xs text-muted-foreground">
              Time window: <span className="font-mono">{timeWindow}</span>
            </p>
          )}
        </section>

        {/* Tool calls */}
        <section className="border-b border-border px-4 py-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Tool calls
          </h3>
          {loading ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          ) : (() => {
            const history = finding?.tool_call_history ?? [];
            const isTerminal = !!finding?.status && TERMINAL_STATUSES.has(finding.status);
            if (history.length === 0) {
              if (isTerminal) {
                return (
                  <p className="text-sm text-muted-foreground">
                    This sub-agent didn&apos;t execute any tools.
                  </p>
                );
              }
              return (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>Waiting for tool activity...</span>
                </div>
              );
            }
            return (
              <ul className="space-y-2">
                {history.map((entry, idx) => {
                  const argsPreview = abbreviateValue(entry.args, 160);
                  const outputPreview = abbreviateValue(entry.output_excerpt, 160);
                  return (
                    <li
                      key={`${entry.tool_name}-${idx}`}
                      className="rounded-md border border-border bg-muted/30 px-2.5 py-2"
                    >
                      <div className="flex items-center gap-2">
                        <ToolCallStatusIcon status={entry.status} />
                        <span className="truncate font-mono text-xs font-medium text-foreground">
                          {entry.tool_name}
                        </span>
                      </div>
                      {argsPreview && (
                        <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                          {argsPreview}
                        </div>
                      )}
                      {outputPreview && (
                        <div className="mt-1 break-all text-[11px] text-muted-foreground">
                          {outputPreview}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            );
          })()}
        </section>

        {/* Findings */}
        <section className="px-4 py-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Findings
          </h3>
          {loading ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-5/6" />
            </div>
          ) : error ? (
            <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
              <AlertCircle className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
              <span className="flex-1 text-muted-foreground">
                Couldn&apos;t load findings
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setReloadKey((k) => k + 1)}
                className="h-7 px-2 text-xs"
              >
                Retry
              </Button>
            </div>
          ) : finding?.body ? (
            <div className="text-sm">
              <MarkdownRenderer content={finding.body} />
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>Waiting for findings...</span>
            </div>
          )}
        </section>
      </div>
    </div>
  );
};

export default SubAgentDetailPanel;
