'use client';

import { useState, useEffect, useCallback, useMemo, memo } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  MinusCircle,
} from 'lucide-react';
import { MarkdownRenderer } from '@/components/ui/markdown-renderer';
import { stripFindingsFrontMatter } from '@/lib/findings-markdown';
import { formatRoleName } from '@/lib/sub-agent-format';
import type { ToolCallHistoryEntry } from '@/components/chat/subagent-detail-panel';
import ToolCallWidget from '@/components/tool-calls/ToolCallWidget';
import { historyEntryId, historyEntryToToolCall } from '@/components/tool-calls/history';

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
  onHasFindings?: (hasFindings: boolean) => void;
}

interface SubAgentInvestigationRowProps {
  finding: Finding;
  incidentId: string;
}

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status as FindingStatus);
}

function StatusIcon({ status }: Readonly<{ status: FindingStatus }>) {
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

function StrengthChip({ strength }: Readonly<{ strength: FindingStrength }>) {
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

const SubAgentInvestigationRow = memo(function SubAgentInvestigationRow({
  finding,
  incidentId,
}: SubAgentInvestigationRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<FindingDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [showFullBody, setShowFullBody] = useState(false);
  const [expandedToolIds, setExpandedToolIds] = useState<Set<string>>(() => new Set());

  const setToolExpanded = useCallback((id: string, isExpanded: boolean) => {
    setExpandedToolIds((prev) => {
      const has = prev.has(id);
      if (has === isExpanded) return prev;
      const next = new Set(prev);
      if (isExpanded) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

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

  const subtitle =
    finding.status === 'running'
      ? finding.current_action || 'Investigating...'
      : null;

  const rawBody = detail?.body ?? null;
  const body = useMemo(
    () => (rawBody ? stripFindingsFrontMatter(rawBody) : null),
    [rawBody],
  );
  const bodyTruncated =
    body && body.length > FINDINGS_BODY_PREVIEW_LIMIT && !showFullBody
      ? `${body.slice(0, FINDINGS_BODY_PREVIEW_LIMIT)}...`
      : body;

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-900/30">
      <button
        type="button"
        aria-expanded={expanded}
        aria-label={`Sub-agent ${finding.role_name}`}
        onClick={toggleExpand}
        className="flex w-full cursor-pointer items-start gap-2 px-2.5 py-2 text-left hover:bg-zinc-800/40 focus:outline-none focus:ring-1 focus:ring-zinc-700"
      >
        <span className="mt-0.5">
          <StatusIcon status={finding.status} />
        </span>
        <span className="block min-w-0 flex-1">
          <span className="mr-1.5 inline-block rounded-sm border border-zinc-700 bg-zinc-800/40 px-1.5 py-0.5 align-middle text-[10px] font-medium tracking-wide text-zinc-300">
            {formatRoleName(finding.role_name)}
          </span>
          <span
            className="break-words align-middle text-xs text-zinc-300 [overflow-wrap:anywhere]"
            title={finding.purpose}
          >
            {finding.purpose}
          </span>
          {subtitle && (
            <span className="mt-0.5 block break-words text-[11px] text-zinc-500 [overflow-wrap:anywhere]">
              {subtitle}
            </span>
          )}
        </span>
        {finding.self_assessed_strength && isParentTerminal && (
          <span className="mt-0.5">
            <StrengthChip strength={finding.self_assessed_strength} />
          </span>
        )}
        <span className="mt-0.5">
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-zinc-500" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-zinc-500" />
          )}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-zinc-800 px-3 py-3">
          {/* Header recap */}
          <div className="mb-3">
            <div className="text-xs font-medium text-zinc-200">{formatRoleName(finding.role_name)}</div>
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
                <div className="space-y-1.5">
                  {history.map((entry, idx) => {
                    const id = historyEntryId(entry, idx);
                    const tool = historyEntryToToolCall(entry, id, expandedToolIds.has(id));
                    return (
                      <ToolCallWidget
                        key={id}
                        tool={tool}
                        onToolUpdate={(patch) => {
                          if (typeof patch.isExpanded === 'boolean') {
                            setToolExpanded(id, patch.isExpanded);
                          }
                        }}
                      />
                    );
                  })}
                </div>
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
  onHasFindings,
}: Readonly<SubAgentInvestigationsSectionProps>) {
  const [findings, setFindings] = useState<Finding[]>([]);

  // Notify parent whenever the presence of findings changes so the parent can
  // gate its own empty-state UI without duplicating the data fetch.
  useEffect(() => {
    onHasFindings?.(findings.length > 0);
  }, [findings.length, onHasFindings]);

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
