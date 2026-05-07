"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { AlertCircle, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { MarkdownRenderer } from "@/components/ui/markdown-renderer";
import { stripFindingsFrontMatter } from "@/lib/findings-markdown";
import { formatRoleName } from "@/lib/sub-agent-format";
import ToolCallWidget from "@/components/tool-calls/ToolCallWidget";
import { historyEntryId, historyEntryToToolCall } from "@/components/tool-calls/history";

interface SubAgentDetailPanelProps {
  incidentId: string;
  agentId: string;
  roleName?: string;
  purpose?: string;
  onClose: () => void;
  className?: string;
}

const PANEL_WIDTH_STORAGE_KEY = "subagent-panel-width";
const PANEL_WIDTH_DEFAULT = 480;
const PANEL_WIDTH_MIN = 360;
const PANEL_WIDTH_MAX_RATIO = 0.7;

function readStoredPanelWidth(): number {
  if (typeof window === "undefined") return PANEL_WIDTH_DEFAULT;
  const raw = window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
  const parsed = raw ? Number(raw) : Number.NaN;
  return Number.isFinite(parsed) && parsed >= PANEL_WIDTH_MIN ? parsed : PANEL_WIDTH_DEFAULT;
}

function clampPanelWidth(value: number): number {
  if (typeof window === "undefined") return Math.max(PANEL_WIDTH_MIN, value);
  const max = Math.max(PANEL_WIDTH_MIN, Math.floor(window.innerWidth * PANEL_WIDTH_MAX_RATIO));
  return Math.max(PANEL_WIDTH_MIN, Math.min(value, max));
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
  const [expandedToolIds, setExpandedToolIds] = React.useState<Set<string>>(() => new Set());

  const [panelWidth, setPanelWidth] = React.useState<number>(PANEL_WIDTH_DEFAULT);
  const panelWidthRef = React.useRef<number>(PANEL_WIDTH_DEFAULT);
  const resizeStateRef = React.useRef<{ startX: number; startWidth: number } | null>(null);

  React.useEffect(() => {
    panelWidthRef.current = panelWidth;
  }, [panelWidth]);

  React.useEffect(() => {
    setPanelWidth(clampPanelWidth(readStoredPanelWidth()));
  }, []);

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const onWindowResize = () => setPanelWidth((w) => clampPanelWidth(w));
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, []);

  const handleResizeStart = React.useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    resizeStateRef.current = { startX: e.clientX, startWidth: panelWidthRef.current };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev: PointerEvent) => {
      const state = resizeStateRef.current;
      if (!state) return;
      setPanelWidth(clampPanelWidth(state.startWidth + (state.startX - ev.clientX)));
    };
    const cleanup = () => {
      resizeStateRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      try {
        window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(panelWidthRef.current));
      } catch { /* ignore quota errors */ }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
  }, []);

  const setToolExpanded = React.useCallback((id: string, isExpanded: boolean) => {
    setExpandedToolIds((prev) => {
      const has = prev.has(id);
      if (has === isExpanded) return prev;
      const next = new Set(prev);
      if (isExpanded) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

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
          // 404 is expected while running and findings don't exist yet.
          // Treat as "not ready yet" silently on both initial and subsequent
          // polls so transient absence doesn't surface as an error.
          if (res.status === 404) {
            if (isInitial) setFinding(null);
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

  const rawRole = finding?.role_name || roleName;
  const displayRole = rawRole ? formatRoleName(rawRole) : agentId;
  const displayPurpose = purpose || "";
  const timeWindow = finding?.time_window;

  return (
    <aside
      className={cn(
        "relative flex h-full flex-shrink-0 flex-col overflow-hidden border-l border-border bg-background",
        className,
      )}
      style={{ width: panelWidth }}
      aria-label="Sub-agent details"
    >
      {/* Resize handle on left edge */}
      <div
        role="separator"
        aria-label="Resize sub-agent panel"
        aria-orientation="vertical"
        onPointerDown={handleResizeStart}
        className="absolute left-0 top-0 h-full w-1.5 -translate-x-1/2 cursor-col-resize bg-transparent hover:bg-orange-500/40 transition-colors z-30"
      />

      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2 flex-shrink-0">
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
              <div className="space-y-2">
                {history.map((entry, idx) => {
                  const id = historyEntryId(entry, idx);
                  const tool = historyEntryToToolCall(entry, id, expandedToolIds.has(id));
                  return (
                    <ToolCallWidget
                      key={id}
                      tool={tool}
                      onToolUpdate={(patch) => {
                        if (typeof patch.isExpanded === "boolean") {
                          setToolExpanded(id, patch.isExpanded);
                        }
                      }}
                    />
                  );
                })}
              </div>
            );
          })()}
        </section>

        {/* Findings */}
        <section className="px-4 py-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Findings
          </h3>
          {(() => {
            if (loading) {
              return (
                <div className="space-y-2">
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-5/6" />
                </div>
              );
            }
            if (error) {
              return (
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
              );
            }
            if (finding?.body) {
              return (
                <div className="text-sm">
                  <MarkdownRenderer content={stripFindingsFrontMatter(finding.body)} />
                </div>
              );
            }
            return (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Waiting for findings...</span>
              </div>
            );
          })()}
        </section>
      </div>
    </aside>
  );
};

export default SubAgentDetailPanel;
