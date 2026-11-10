'use client';

import { Fragment, useEffect, useState } from 'react';
import Link from 'next/link';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Clock,
  ExternalLink, Loader2, MessageSquare, Search, Siren, Wrench, X,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ChartPanel, EmptyState, StatCard, StatCardSkeleton } from './charts';

type Status = 'failed' | 'all' | 'succeeded';
type Window = '24h' | '7d' | '30d' | '90d';
type Source = 'all' | 'chat' | 'incident';

interface ToolCall {
  id: number;
  tool_name: string;
  provider: string | null;
  input_summary: string;
  status: 'failed' | 'succeeded' | 'running' | string;
  reason: string;
  error: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  source: { type: 'chat' | 'incident'; id: string | null; title: string; url: string | null };
}

interface ToolFacet { tool_name: string; total: number; failed: number }

interface ToolCallsResponse {
  items: ToolCall[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
  summary: { total: number; failed: number; succeeded: number; running: number };
  tools: ToolFacet[];
}

const STATUSES: { value: Status; label: string }[] = [
  { value: 'failed', label: 'Failed' },
  { value: 'all', label: 'All' },
  { value: 'succeeded', label: 'Succeeded' },
];

const WINDOWS: { value: Window; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
];

const WINDOW_LABEL: Record<Window, string> = {
  '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days', '90d': 'last 90 days',
};

const PER_PAGE = 25;

function Segmented<T extends string>({
  options, value, onChange, label,
}: { options: { value: T; label: string }[]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="flex rounded-lg border border-zinc-800 overflow-hidden">
      {options.map(o => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
          className={`px-3 py-1 text-xs font-medium transition-all duration-200 ${
            value === o.value
              ? 'bg-zinc-700/80 text-zinc-100'
              : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function formatMs(ms: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

function StatusBadge({ status }: { status: string }) {
  if (status === 'failed') {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-red-500/10 text-red-400">
        <AlertTriangle className="h-3 w-3" /> Failed
      </span>
    );
  }
  if (status === 'succeeded') {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400">
        <CheckCircle2 className="h-3 w-3" /> OK
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-zinc-500/10 text-zinc-400">
      <Loader2 className="h-3 w-3" /> {status}
    </span>
  );
}

// Only follow app-relative links the backend built; never an arbitrary scheme.
function safeHref(url: string | null): string | null {
  return url && url.startsWith('/') && !url.startsWith('//') ? url : null;
}

function SourceLink({ source }: { source: ToolCall['source'] }) {
  const Icon = source.type === 'incident' ? Siren : MessageSquare;
  const href = safeHref(source.url);
  const body = (
    <>
      <Icon className="h-3 w-3 shrink-0" />
      <span className="truncate">{source.title}</span>
      {href && <ExternalLink className="h-3 w-3 shrink-0 opacity-0 group-hover:opacity-100" />}
    </>
  );
  if (!href) return <span className="flex items-center gap-1.5 text-xs text-zinc-500 max-w-[220px]">{body}</span>;
  return (
    <Link
      href={href}
      onClick={(e) => e.stopPropagation()}
      className="group flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200 max-w-[220px] transition-colors"
    >
      {body}
    </Link>
  );
}

function FailuresByTool({
  tools, selected, onSelect,
}: { tools: ToolFacet[]; selected: string; onSelect: (t: string) => void }) {
  const failing = tools.filter(t => t.failed > 0).slice(0, 8);
  if (!failing.length) return null;
  const max = Math.max(...failing.map(t => t.failed));
  return (
    <ChartPanel title="Failures by tool" subtitle="Click a tool to filter">
      <ul className="space-y-2">
        {failing.map(t => {
          const rate = t.total ? Math.round((100 * t.failed) / t.total) : 0;
          const active = selected === t.tool_name;
          return (
            <li key={t.tool_name}>
              <button
                onClick={() => onSelect(active ? '' : t.tool_name)}
                aria-pressed={active}
                className={`w-full text-left rounded-md px-2 py-1.5 transition-colors ${active ? 'bg-zinc-800/80' : 'hover:bg-zinc-800/40'}`}
              >
                <div className="flex items-center justify-between text-xs mb-1">
                  <span className="font-mono text-zinc-300 truncate">{t.tool_name}</span>
                  <span className="text-zinc-500 shrink-0 ml-2" style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {t.failed} / {t.total} · {rate}%
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                  <div className="h-full bg-red-500/70 rounded-full" style={{ width: `${(100 * t.failed) / max}%` }} />
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </ChartPanel>
  );
}

export default function ToolCallsTab() {
  const [status, setStatus] = useState<Status>('failed');
  const [timeWindow, setTimeWindow] = useState<Window>('7d');
  const [source, setSource] = useState<Source>('all');
  const [tool, setTool] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState<number | null>(null);
  const q = useDebounced(search.trim(), 300);

  // Any filter change returns to page 1 and collapses the open row.
  useEffect(() => { setPage(1); setExpanded(null); }, [status, timeWindow, source, tool, q]);

  const params = new URLSearchParams({ status, period: timeWindow, page: String(page), per_page: String(PER_PAGE) });
  if (source !== 'all') params.set('source', source);
  if (tool) params.set('tool', tool);
  if (q) params.set('q', q);

  const { data, error, isLoading } = useQuery<ToolCallsResponse>(
    `/api/monitor/tool-calls?${params.toString()}`,
    jsonFetcher,
    { staleTime: 15_000 },
  );

  const summary = data?.summary;
  const completed = summary ? summary.failed + summary.succeeded : 0;
  const failureRate = completed ? ((100 * (summary?.failed ?? 0)) / completed).toFixed(1) : '0';
  const topFailing = data?.tools.find(t => t.failed > 0);
  const hasFilters = source !== 'all' || tool || search;

  return (
    <div className="space-y-6">
      {/* Summary */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {!summary ? (
          Array.from({ length: 4 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : (
          <>
            <StatCard label="Tool calls" value={summary.total.toLocaleString()} sub={WINDOW_LABEL[timeWindow]} icon={Wrench} />
            <StatCard label="Failed" value={summary.failed.toLocaleString()} sub={`${failureRate}% of completed calls`} icon={AlertTriangle} />
            <StatCard label="Succeeded" value={summary.succeeded.toLocaleString()} sub={summary.running ? `${summary.running} running / unfinished` : undefined} icon={CheckCircle2} />
            <StatCard
              label="Most failing tool"
              value={topFailing ? String(topFailing.failed) : '—'}
              sub={topFailing ? topFailing.tool_name : 'No failures'}
              icon={Clock}
            />
          </>
        )}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_300px] gap-6 items-start">
        <ChartPanel
          title="Tool calls"
          subtitle="Every tool the agent ran in chats and RCA investigations. Secrets in inputs and errors are redacted."
          loading={isLoading}
          className="min-w-0"
        >
          {/* Filters */}
          <div className="flex flex-wrap items-center gap-2 mb-4">
            <Segmented label="Status" options={STATUSES} value={status} onChange={setStatus} />
            <Segmented label="Time window" options={WINDOWS} value={timeWindow} onChange={setTimeWindow} />
            <select
              aria-label="Source"
              value={source}
              onChange={(e) => setSource(e.target.value as Source)}
              className="bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2 py-1 text-xs text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-600"
            >
              <option value="all">Chats &amp; incidents</option>
              <option value="chat">Chats only</option>
              <option value="incident">Incidents only</option>
            </select>
            <select
              aria-label="Tool"
              value={tool}
              onChange={(e) => setTool(e.target.value)}
              className="bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2 py-1 text-xs text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-600 max-w-[180px]"
            >
              <option value="">All tools</option>
              {tool && !data?.tools.some(t => t.tool_name === tool) && <option value={tool}>{tool}</option>}
              {data?.tools.map(t => (
                <option key={t.tool_name} value={t.tool_name}>{t.tool_name} ({t.total})</option>
              ))}
            </select>
            <div className="relative flex-1 min-w-[180px]">
              <Search className="h-3.5 w-3.5 text-zinc-500 absolute left-2 top-1/2 -translate-y-1/2" />
              <input
                type="search"
                placeholder="Search command or error (e.g. AccessDenied)"
                aria-label="Search command or error"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                maxLength={200}
                className="w-full bg-zinc-800/50 border border-zinc-700/50 rounded-md pl-7 pr-2.5 py-1 text-xs text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-600"
              />
            </div>
            {hasFilters && (
              <button
                onClick={() => { setSource('all'); setTool(''); setSearch(''); }}
                className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
              >
                <X className="h-3 w-3" /> Clear
              </button>
            )}
          </div>

          {error && !data ? (
            <EmptyState icon={AlertTriangle} message="Could not load tool calls" hint="Try again in a moment." />
          ) : !data || data.items.length === 0 ? (
            isLoading ? (
              <div className="h-40 animate-pulse bg-zinc-800/30 rounded-lg" />
            ) : (
              <EmptyState
                icon={Wrench}
                message={status === 'failed' ? `No failed tool calls in the ${WINDOW_LABEL[timeWindow]}` : 'No tool calls match these filters'}
                hint="Tool calls from chats are recorded from this release onward; RCA tool calls include earlier history."
              />
            )
          ) : (
            <>
              <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
                <table className="w-full text-sm table-fixed min-w-[860px]">
                  <colgroup>
                    <col className="w-[120px]" />
                    <col className="w-[150px]" />
                    <col />
                    <col className="w-[30%]" />
                    <col className="w-[190px]" />
                    <col className="w-[28px]" />
                  </colgroup>
                  <thead>
                    <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                      <th className="text-left px-3 py-2.5 font-medium">Time</th>
                      <th className="text-left px-3 py-2.5 font-medium">Tool</th>
                      <th className="text-left px-3 py-2.5 font-medium">Input</th>
                      <th className="text-left px-3 py-2.5 font-medium">{status === 'succeeded' ? 'Status' : 'Reason'}</th>
                      <th className="text-left px-3 py-2.5 font-medium">From</th>
                      <th aria-label="Details" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(tc => {
                      const open = expanded === tc.id;
                      return (
                        <Fragment key={tc.id}>
                          <tr
                            onClick={() => setExpanded(open ? null : tc.id)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(open ? null : tc.id); }
                            }}
                            tabIndex={0}
                            aria-expanded={open}
                            className="border-b border-zinc-800/40 hover:bg-zinc-800/20 cursor-pointer transition-colors duration-150 align-top focus:outline-none focus-visible:bg-zinc-800/30"
                          >
                            <td className="px-3 py-2.5 text-zinc-500 text-xs whitespace-nowrap" style={{ fontVariantNumeric: 'tabular-nums' }}>
                              {formatTimestamp(tc.started_at)}
                            </td>
                            <td className="px-3 py-2.5">
                              <div className="font-mono text-xs text-zinc-300 truncate" title={tc.tool_name}>{tc.tool_name}</div>
                              {tc.status !== 'failed' && <div className="mt-1"><StatusBadge status={tc.status} /></div>}
                            </td>
                            <td className="px-3 py-2.5">
                              <div className="font-mono text-xs text-zinc-400 truncate" title={tc.input_summary}>
                                {tc.input_summary || '—'}
                              </div>
                            </td>
                            <td className="px-3 py-2.5">
                              {tc.status === 'failed' ? (
                                <div className="text-xs text-red-300/90 line-clamp-2 break-words" title={tc.reason}>{tc.reason || 'Failed (no error text)'}</div>
                              ) : (
                                <span className="text-xs text-zinc-500">{formatMs(tc.duration_ms)}</span>
                              )}
                            </td>
                            <td className="px-3 py-2.5"><SourceLink source={tc.source} /></td>
                            <td className="px-1 py-2.5 text-zinc-500">
                              <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
                            </td>
                          </tr>
                          {open && (
                            <tr className="border-b border-zinc-800/40 bg-zinc-950/40">
                              <td colSpan={6} className="px-4 py-4">
                                <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-zinc-500 mb-3">
                                  <StatusBadge status={tc.status} />
                                  <span>Started {tc.started_at ? new Date(tc.started_at).toLocaleString() : '—'}</span>
                                  <span>Duration {formatMs(tc.duration_ms)}</span>
                                  {tc.provider && <span>Provider {tc.provider}</span>}
                                  <span className="capitalize">{tc.source.type}</span>
                                </div>
                                <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Input</p>
                                <pre className="text-xs font-mono text-zinc-300 bg-zinc-900/80 border border-zinc-800/60 rounded-md p-3 whitespace-pre-wrap break-all max-h-48 overflow-auto">
                                  {tc.input_summary || '—'}
                                </pre>
                                {tc.error && (
                                  <>
                                    <p className="text-[11px] uppercase tracking-wider text-zinc-500 mt-3 mb-1">Error</p>
                                    <pre className="text-xs font-mono text-red-200/90 bg-red-950/20 border border-red-900/30 rounded-md p-3 whitespace-pre-wrap break-all max-h-72 overflow-auto">
                                      {tc.error}
                                    </pre>
                                  </>
                                )}
                                {safeHref(tc.source.url) && (
                                  <Link
                                    href={safeHref(tc.source.url) as string}
                                    className="inline-flex items-center gap-1.5 mt-3 text-xs text-zinc-300 hover:text-zinc-100 transition-colors"
                                  >
                                    Open {tc.source.type === 'incident' ? 'incident' : 'chat'} <ExternalLink className="h-3 w-3" />
                                  </Link>
                                )}
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="flex items-center justify-between mt-4">
                <p className="text-xs text-zinc-500">
                  {data.total.toLocaleString()} {data.total === 1 ? 'call' : 'calls'} — page {data.page} of {data.total_pages}
                </p>
                {data.total_pages > 1 && (
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setPage(p => Math.max(1, p - 1))}
                      disabled={page <= 1}
                      aria-label="Previous page"
                      className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => setPage(p => Math.min(data.total_pages, p + 1))}
                      disabled={page >= data.total_pages}
                      aria-label="Next page"
                      className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </button>
                  </div>
                )}
              </div>
            </>
          )}
        </ChartPanel>

        {data && <FailuresByTool tools={data.tools} selected={tool} onSelect={setTool} />}
      </div>
    </div>
  );
}
