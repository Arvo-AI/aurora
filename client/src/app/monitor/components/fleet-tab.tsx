'use client';

import { useState, useCallback } from 'react';
import {
  Activity, CheckCircle2, XCircle, Clock, Loader2,
  ChevronDown, ChevronRight, Zap, AlertTriangle, Lightbulb,
  ExternalLink, Flame, ShieldAlert,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import {
  StatCard, StatCardSkeleton, ChartPanel, EmptyState,
  formatDuration, type Period,
} from './charts';

interface FleetSummary {
  total_agent_runs: number;
  active_count: number;
  completed_count: number;
  error_count: number;
  avg_rca_duration_seconds: number | null;
  critical_count: number;
  high_count: number;
}

interface FleetRun {
  incident_id: string;
  alert_title: string | null;
  alert_service: string | null;
  aurora_status: string;
  severity: string | null;
  source_type: string;
  started_at: string;
  analyzed_at: string | null;
  updated_at: string;
  incident_status: string | null;
  aurora_summary: string | null;
  duration_seconds: number | null;
  session_id: string;
  suggestion_count: number;
  suggestion_titles: string | null;
  correlated_alert_count: number | null;
}

interface ActivityEvent {
  event_type: string;
  label: string;
  status: string;
  event_time: string;
  duration_ms: number | null;
  detail: string | null;
  error_message: string | null;
}

const STATUS_FILTERS = [
  { label: 'All', value: '' },
  { label: 'Running', value: 'running' },
  { label: 'Complete', value: 'complete' },
  { label: 'Error', value: 'error' },
];

function statusBadge(status: string) {
  const map: Record<string, { bg: string; text: string; dot: string; label: string }> = {
    running:     { bg: 'bg-blue-500/10',    text: 'text-blue-400',    dot: 'bg-blue-400',    label: 'Running' },
    analyzing:   { bg: 'bg-blue-500/10',    text: 'text-blue-400',    dot: 'bg-blue-400 animate-pulse', label: 'Analyzing' },
    summarizing: { bg: 'bg-blue-500/10',    text: 'text-blue-400',    dot: 'bg-blue-400 animate-pulse', label: 'Summarizing' },
    pending:     { bg: 'bg-zinc-500/10',    text: 'text-zinc-400',    dot: 'bg-zinc-400',    label: 'Pending' },
    complete:    { bg: 'bg-emerald-500/10',  text: 'text-emerald-400', dot: 'bg-emerald-400', label: 'Complete' },
    completed:   { bg: 'bg-emerald-500/10',  text: 'text-emerald-400', dot: 'bg-emerald-400', label: 'Complete' },
    analyzed:    { bg: 'bg-emerald-500/10',  text: 'text-emerald-400', dot: 'bg-emerald-400', label: 'Analyzed' },
    resolved:    { bg: 'bg-emerald-500/10',  text: 'text-emerald-400', dot: 'bg-emerald-400', label: 'Resolved' },
    error:       { bg: 'bg-red-500/10',      text: 'text-red-400',     dot: 'bg-red-400',     label: 'Error' },
    idle:        { bg: 'bg-zinc-500/10',     text: 'text-zinc-400',    dot: 'bg-zinc-400',    label: 'Idle' },
  };
  const s = map[status] || map.idle;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium ${s.bg} ${s.text}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
      {s.label}
    </span>
  );
}

function severityBadge(severity: string | null) {
  if (!severity) return null;
  const colors: Record<string, string> = {
    critical: 'bg-red-500/15 text-red-400 ring-red-500/20',
    high: 'bg-orange-500/15 text-orange-400 ring-orange-500/20',
    medium: 'bg-yellow-500/15 text-yellow-400 ring-yellow-500/20',
    low: 'bg-blue-500/15 text-blue-400 ring-blue-500/20',
  };
  return (
    <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ring-1 ${colors[severity] || 'bg-zinc-500/15 text-zinc-400 ring-zinc-500/20'}`}>
      {severity}
    </span>
  );
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export default function FleetTab({ period }: { period: Period }) {
  const [statusFilter, setStatusFilter] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const timeRange = period;

  const { data: summary, isLoading: summaryLoading } = useQuery<FleetSummary>(
    `/api/monitor/fleet/summary?time_range=${timeRange}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const fleetUrl = `/api/monitor/fleet?time_range=${timeRange}${statusFilter ? `&status=${statusFilter}` : ''}`;
  const { data: runs, isLoading: runsLoading } = useQuery<FleetRun[]>(
    fleetUrl,
    jsonFetcher,
    { staleTime: 15_000 },
  );

  const toggleExpand = useCallback((id: string) => {
    setExpandedId(prev => prev === id ? null : id);
  }, []);

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {summaryLoading ? (
          Array.from({ length: 5 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : summary ? (
          <>
            <StatCard label="Total Runs" value={String(summary.total_agent_runs)} icon={Activity} />
            <StatCard label="Active" value={String(summary.active_count)} icon={Loader2} sub="currently running" />
            <StatCard label="Completed" value={String(summary.completed_count)} icon={CheckCircle2} />
            <StatCard label="Errors" value={String(summary.error_count)} icon={XCircle} />
            <StatCard label="Avg RCA Time" value={formatDuration(summary.avg_rca_duration_seconds)} icon={Clock} sub={summary.critical_count > 0 ? `${summary.critical_count} critical` : undefined} />
          </>
        ) : null}
      </div>

      {/* Agent Runs */}
      <ChartPanel title="Agent Runs" loading={runsLoading}>
        <div className="flex items-center gap-1 mb-4">
          {STATUS_FILTERS.map(f => (
            <button
              key={f.value}
              onClick={() => setStatusFilter(f.value)}
              className={`px-2.5 py-1 text-xs font-medium rounded-md transition-all duration-200 ${
                statusFilter === f.value
                  ? 'bg-zinc-700/80 text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {!runs || runs.length === 0 ? (
          <EmptyState icon={Zap} message="No agent runs found for this period" hint="Runs appear when Aurora investigates incidents" />
        ) : (
          <div className="space-y-2">
            {runs.map(run => (
              <RunCard
                key={run.incident_id}
                run={run}
                expanded={expandedId === run.incident_id}
                onToggle={() => toggleExpand(run.incident_id)}
              />
            ))}
          </div>
        )}
      </ChartPanel>
    </div>
  );
}

function RunCard({ run, expanded, onToggle }: { run: FleetRun; expanded: boolean; onToggle: () => void }) {
  const hasResolution = run.aurora_summary || (run.suggestion_count > 0);
  const isActive = ['running', 'analyzing', 'summarizing', 'pending'].includes(run.aurora_status);

  return (
    <div className={`rounded-lg border transition-all duration-200 ${
      expanded ? 'border-zinc-700/60 bg-zinc-800/20' : 'border-zinc-800/50 hover:border-zinc-700/40 bg-zinc-900/30'
    }`}>
      {/* Main row */}
      <button
        onClick={onToggle}
        className="w-full text-left px-4 py-3 flex items-start gap-3"
      >
        <div className="mt-0.5 text-zinc-600 shrink-0">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </div>

        <div className="flex-1 min-w-0">
          {/* Title row */}
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-zinc-200 truncate max-w-[400px]">
              {run.alert_title || run.alert_service || 'Untitled incident'}
            </span>
            {statusBadge(run.aurora_status)}
            {severityBadge(run.severity)}
            {(run.correlated_alert_count ?? 0) > 1 && (
              <span className="text-[10px] text-zinc-500 font-medium px-1.5 py-0.5 bg-zinc-800/60 rounded">
                {run.correlated_alert_count} alerts
              </span>
            )}
          </div>

          {/* Meta line */}
          <div className="flex items-center gap-3 mt-1 text-xs text-zinc-500">
            <span>{run.source_type}</span>
            {run.alert_service && (
              <>
                <span className="text-zinc-700">·</span>
                <span className="text-zinc-400">{run.alert_service}</span>
              </>
            )}
            <span className="text-zinc-700">·</span>
            <span>{timeAgo(run.started_at)}</span>
            {run.duration_seconds != null && (
              <>
                <span className="text-zinc-700">·</span>
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {formatDuration(run.duration_seconds)}
                </span>
              </>
            )}
            {run.suggestion_count > 0 && (
              <>
                <span className="text-zinc-700">·</span>
                <span className="flex items-center gap-1 text-amber-400/80">
                  <Lightbulb className="h-3 w-3" />
                  {run.suggestion_count} fix{run.suggestion_count !== 1 ? 'es' : ''}
                </span>
              </>
            )}
          </div>

          {/* Summary preview (collapsed) */}
          {!expanded && run.aurora_summary && (
            <p className="text-xs text-zinc-500 mt-1.5 line-clamp-1 leading-relaxed">
              {run.aurora_summary}
            </p>
          )}
        </div>
      </button>

      {/* Expanded section */}
      {expanded && (
        <div className="px-4 pb-4 pt-0 ml-7 space-y-3">
          {/* Resolution / Summary */}
          {run.aurora_summary && (
            <div className="rounded-lg bg-zinc-900/60 border border-zinc-800/50 p-3">
              <div className="flex items-center gap-1.5 mb-2">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500/70" />
                <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">Resolution Summary</span>
              </div>
              <p className="text-sm text-zinc-300 leading-relaxed whitespace-pre-line">
                {run.aurora_summary}
              </p>
            </div>
          )}

          {/* Suggestions */}
          {run.suggestion_titles && (
            <div className="rounded-lg bg-zinc-900/60 border border-zinc-800/50 p-3">
              <div className="flex items-center gap-1.5 mb-2">
                <Lightbulb className="h-3.5 w-3.5 text-amber-400/70" />
                <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">Suggested Fixes</span>
              </div>
              <div className="space-y-1">
                {run.suggestion_titles.split(' | ').map((title, i) => (
                  <div key={i} className="flex items-start gap-2 text-sm text-zinc-300">
                    <span className="text-zinc-600 mt-0.5">→</span>
                    <span>{title}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Activity timeline */}
          <ActivitySection incidentId={run.incident_id} />

          {/* Link to incident */}
          <a
            href={`/incidents?id=${run.incident_id}`}
            className="inline-flex items-center gap-1.5 text-xs text-blue-400/80 hover:text-blue-400 transition-colors"
          >
            <ExternalLink className="h-3 w-3" />
            View full incident
          </a>
        </div>
      )}
    </div>
  );
}

function ActivitySection({ incidentId }: { incidentId: string }) {
  const { data: events, isLoading } = useQuery<ActivityEvent[]>(
    `/api/monitor/fleet/${incidentId}/activity`,
    (key, signal) => fetch(key, { credentials: 'include', signal }).then(r => {
      if (!r.ok) throw new Error(`${r.status}`);
      return r.json();
    }),
    { staleTime: 15_000 },
  );

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-zinc-500 text-xs py-2">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading activity...
      </div>
    );
  }

  if (!events?.length) return null;

  const toolEvents = events.filter(e => e.event_type === 'execution_step' || e.event_type === 'citation');
  const display = toolEvents.length > 0 ? toolEvents.slice(-12) : events.slice(-8);

  return (
    <div className="rounded-lg bg-zinc-900/60 border border-zinc-800/50 p-3">
      <div className="flex items-center gap-1.5 mb-2">
        <Activity className="h-3.5 w-3.5 text-blue-400/70" />
        <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">
          Tool Activity
          <span className="text-zinc-600 font-normal ml-1">({events.length} events)</span>
        </span>
      </div>
      <div className="space-y-1 max-h-48 overflow-y-auto">
        {display.map((evt, i) => (
          <div key={i} className="flex items-center gap-2.5 text-xs group">
            <span className="text-zinc-600 w-14 shrink-0 text-right" style={{ fontVariantNumeric: 'tabular-nums' }}>
              {evt.event_time
                ? new Date(evt.event_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                : '—'}
            </span>
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              evt.status === 'error' ? 'bg-red-400'
              : evt.status === 'success' || evt.status === 'complete' ? 'bg-emerald-400'
              : 'bg-zinc-500'
            }`} />
            <span className="text-zinc-300 font-mono truncate">{evt.label}</span>
            {evt.duration_ms != null && (
              <span className="text-zinc-600 shrink-0">{evt.duration_ms}ms</span>
            )}
            {evt.error_message && (
              <span className="text-red-400/80 flex items-center gap-1 truncate">
                <AlertTriangle className="h-3 w-3 shrink-0" /> {evt.error_message.slice(0, 60)}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
