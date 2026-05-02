'use client';

import { useState, useEffect, useCallback, memo, type KeyboardEvent } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  MinusCircle,
} from 'lucide-react';
import { MarkdownRenderer } from '@/components/ui/markdown-renderer';
import type { ToolCallHistoryEntry } from '@/components/chat/subagent-detail-panel';

const POLL_INTERVAL_MS = 3000;
const FINDINGS_BODY_PREVIEW_LIMIT = 600;

const TERMINAL_STATUSES = new Set<FindingStatus>([
  'succeeded',
  'failed',
  'timeout',
  'cancelled',
  'inconclusive',
]);

type FindingStatus =
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'inconclusive';

type FindingStrength = 'strong' | 'moderate' | 'weak' | 'inconclusive';

export interface Finding {
  agent_id: string;
  role_name: string;
  purpose: string;
  status: FindingStatus;
  wave?: number;
  self_assessed_strength?: FindingStrength;
  current_action?: string | null;
  child_session_id?: string;
  started_at?: string;
  completed_at?: string;
  tools_used?: string[];
  citations?: unknown[];
  follow_ups_suggested?: unknown[];
}

interface FindingDetail {
  agent_id: string;
  status: string;
  body: string | null;
  tool_call_history: ToolCallHistoryEntry[];
}

interface FindingsListResponse {
  findings: Finding[];
}

interface SubAgentInvestigationsSectionProps {
  incidentId: string;
  isActive: boolean;
}

interface SubAgentInvestigationRowProps {
  finding: Finding;
  incidentId: string;
}

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status as FindingStatus);
}

function StatusIcon({ status }: { status: FindingStatus }) {
  if (status === 'running') {
    return <Loader2 className="h-3.5 w-3.5 flex-shrink-0 animate-spin text-orange-400" />;
  }
  if (status === 'succeeded') {
    return <CheckCircle2 className="h-3.5 w-3.5 flex-shrink-0 text-emerald-500" />;
  }
  if (status === 'failed' || status === 'timeout' || status === 'cancelled') {
    return <AlertCircle className="h-3.5 w-3.5 flex-shrink-0 text-amber-500" />;
  }
  return <MinusCircle className="h-3.5 w-3.5 flex-shrink-0 text-zinc-500" />;
}

function StrengthChip({ strength }: { strength: FindingStrength }) {
  const tone =
    strength === 'strong'
      ? 'text-emerald-400 border-emerald-400/30'
      : strength === 'moderate'
        ? 'text-zinc-300 border-zinc-600'
        : strength === 'weak'
          ? 'text-amber-400 border-amber-400/30'
          : 'text-zinc-500 border-zinc-700';
  return (
    <span
      className={`rounded-sm border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${tone}`}
    >
      {strength}
    </span>
  );
}

function ToolCallStatusIcon({ status }: { status: string }) {
  if (status === 'running' || status === 'pending') {
    return <Loader2 className="h-3 w-3 flex-shrink-0 animate-spin text-zinc-500" />;
  }
  if (status === 'error' || status === 'failed' || status === 'cancelled') {
    return <AlertCircle className="h-3 w-3 flex-shrink-0 text-amber-500" />;
  }
  return <CheckCircle2 className="h-3 w-3 flex-shrink-0 text-emerald-500" />;
}

function abbreviateValue(value: unknown, max = 160): string {
  if (value === null || value === undefined) return '';
  let s: string;
  if (typeof value === 'string') {
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

const SubAgentInvestigationRow = memo(function SubAgentInvestigationRow({
  finding,
  incidentId,
}: SubAgentInvestigationRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<FindingDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [showFullBody, setShowFullBody] = useState(false);

  const isParentTerminal = isTerminal(finding.status);

  // Fetch detail on expand + poll while running
  useEffect(() => {
    if (!expanded) return;

    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const fetchDetail = async (isInitial: boolean) => {
      if (isInitial) setDetailLoading(true);
      try {
        const res = await fetch(
          `/api/incidents/${incidentId}/findings/${finding.agent_id}`,
          { method: 'GET', cache: 'no-store', credentials: 'include' },
        );
        if (cancelled) return;
        if (!res.ok) {
          if (res.status === 404 && isInitial) {
            setDetail(null);
            setDetailError(null);
            return;
          }
          throw new Error(`Request failed (${res.status})`);
        }
        const data = (await res.json()) as FindingDetail;
        if (cancelled) return;
        setDetail(data);
        setDetailError(null);
        if (data.status && isTerminal(data.status) && intervalId) {
          clearInterval(intervalId);
          intervalId = null;
        }
      } catch (e) {
        if (cancelled) return;
        setDetailError(e instanceof Error ? e.message : 'Failed to load');
      } finally {
        if (!cancelled && isInitial) setDetailLoading(false);
      }
    };

    fetchDetail(true);
    if (!isParentTerminal) {
      intervalId = setInterval(() => fetchDetail(false), POLL_INTERVAL_MS);
    }

    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [expanded, incidentId, finding.agent_id, isParentTerminal]);

  const toggleExpand = useCallback(() => {
    setExpanded((v) => !v);
  }, []);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleExpand();
      }
    },
    [toggleExpand],
  );

  const subtitle =
    finding.status === 'running'
      ? finding.current_action || 'Investigating...'
      : null;

  const body = detail?.body ?? null;
  const bodyTruncated =
    body && body.length > FINDINGS_BODY_PREVIEW_LIMIT && !showFullBody
      ? `${body.slice(0, FINDINGS_BODY_PREVIEW_LIMIT)}...`
      : body;

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-900/30">
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-label={`Sub-agent ${finding.role_name}`}
        onClick={toggleExpand}
        onKeyDown={handleKeyDown}
        className="flex cursor-pointer items-center gap-2 px-2.5 py-2 hover:bg-zinc-800/40 focus:outline-none focus:ring-1 focus:ring-zinc-700"
      >
        <StatusIcon status={finding.status} />
        <span className="rounded-sm border border-zinc-700 bg-zinc-800/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-400">
          {finding.role_name}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs text-zinc-300" title={finding.purpose}>
            {finding.purpose}
          </div>
          {subtitle && (
            <div className="mt-0.5 truncate text-[11px] text-zinc-500">{subtitle}</div>
          )}
        </div>
        {finding.self_assessed_strength && isParentTerminal && (
          <StrengthChip strength={finding.self_assessed_strength} />
        )}
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-zinc-500" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-zinc-500" />
        )}
      </div>

      {expanded && (
        <div className="border-t border-zinc-800 px-3 py-3">
          {/* Header recap */}
          <div className="mb-3">
            <div className="text-xs font-medium text-zinc-200">{finding.role_name}</div>
            {finding.purpose && (
              <p className="mt-0.5 whitespace-pre-wrap text-[11px] text-zinc-500">
                {finding.purpose}
              </p>
            )}
          </div>

          {/* Tool call history */}
          <div className="mb-3">
            <h4 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
              Tool calls
            </h4>
            {detailLoading && !detail ? (
              <p className="text-[11px] text-zinc-500">Loading...</p>
            ) : detailError && !detail ? (
              <p className="text-[11px] text-amber-500">{detailError}</p>
            ) : (() => {
              const history = detail?.tool_call_history ?? [];
              if (history.length === 0) {
                if (isParentTerminal) {
                  return (
                    <p className="text-[11px] text-zinc-500">
                      No tools were executed.
                    </p>
                  );
                }
                return (
                  <div className="flex items-center gap-1.5 text-[11px] text-zinc-500">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    <span>Waiting for tool activity...</span>
                  </div>
                );
              }
              return (
                <ul className="space-y-1.5">
                  {history.map((entry, idx) => {
                    const argsPreview = abbreviateValue(entry.args, 140);
                    const outputPreview = abbreviateValue(entry.output_excerpt, 140);
                    return (
                      <li
                        key={`${entry.tool_name}-${idx}`}
                        className="rounded-sm border border-zinc-800 bg-zinc-900/40 px-2 py-1.5"
                      >
                        <div className="flex items-center gap-1.5">
                          <ToolCallStatusIcon status={entry.status} />
                          <span className="truncate font-mono text-[11px] font-medium text-zinc-300">
                            {entry.tool_name}
                          </span>
                        </div>
                        {argsPreview && (
                          <div className="mt-0.5 break-all font-mono text-[10px] text-zinc-500">
                            {argsPreview}
                          </div>
                        )}
                        {outputPreview && (
                          <div className="mt-0.5 break-all text-[10px] text-zinc-500">
                            {outputPreview}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              );
            })()}
          </div>

          {/* Findings preview */}
          <div>
            <h4 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
              Findings
            </h4>
            {detailLoading && !detail ? (
              <p className="text-[11px] text-zinc-500">Loading...</p>
            ) : body ? (
              <div className="text-xs text-zinc-300">
                <MarkdownRenderer content={bodyTruncated || ''} />
                {body.length > FINDINGS_BODY_PREVIEW_LIMIT && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setShowFullBody((v) => !v);
                    }}
                    className="mt-1 text-[11px] text-zinc-500 hover:text-zinc-300"
                  >
                    {showFullBody ? 'Show less' : 'View full findings'}
                  </button>
                )}
              </div>
            ) : (
              <p className="text-[11px] text-zinc-500">
                Findings will appear when this sub-agent finishes.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

export default function SubAgentInvestigationsSection({
  incidentId,
  isActive,
}: SubAgentInvestigationsSectionProps) {
  const [findings, setFindings] = useState<Finding[]>([]);

  // Poll findings list. Cadence: 3s. The interval callback self-stops once the
  // incident is inactive AND no findings are running, so the closure-driven
  // "should I still poll?" check always reflects the latest server data.
  useEffect(() => {
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;
    let lastSerialized = '';

    const fetchFindings = async () => {
      try {
        const res = await fetch(`/api/incidents/${incidentId}/findings`, {
          method: 'GET',
          cache: 'no-store',
          credentials: 'include',
        });
        if (cancelled) return;
        if (!res.ok) return;
        const data = (await res.json()) as FindingsListResponse;
        if (cancelled) return;
        const next = data.findings ?? [];

        // Skip the setState (and downstream rerender) when nothing changed.
        const serialized = JSON.stringify(next);
        if (serialized !== lastSerialized) {
          lastSerialized = serialized;
          setFindings(next);
        }

        const anyRunning = next.some((f) => f.status === 'running');
        if (!isActive && !anyRunning && intervalId) {
          clearInterval(intervalId);
          intervalId = null;
        }
      } catch {
        // swallow — transient network errors are fine, next tick will retry
      }
    };

    fetchFindings();
    intervalId = setInterval(fetchFindings, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [incidentId, isActive]);

  // Empty-state guard — zero DOM impact for non-fan-out incidents.
  if (findings.length === 0) {
    return null;
  }

  const anyRunning = findings.some((f) => f.status === 'running');

  return (
    <div className="mt-6 border-t border-zinc-800 pt-4">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
          Sub-agent investigations
        </h3>
        <span className="text-xs text-zinc-500">
          · {findings.length} agent{findings.length === 1 ? '' : 's'}
        </span>
        {anyRunning && (
          <span className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-orange-400" />
        )}
      </div>
      <div className="space-y-2">
        {findings.map((finding) => (
          <SubAgentInvestigationRow
            key={finding.agent_id}
            finding={finding}
            incidentId={incidentId}
          />
        ))}
      </div>
    </div>
  );
}
