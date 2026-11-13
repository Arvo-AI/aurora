'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { AlertTriangle, ExternalLink, Wrench, X } from 'lucide-react';
import { useQuery } from '@/lib/query';
import { ChartPanel, EmptyState, type Period } from './charts';
import { FilterError, LiveStatus, Pager, SearchBox, UserFilter, auditFetcher, filterErrorOf, selectCls, useDebounced, useKeptData } from './audit-controls';
import {
  ISSUE_ORDER, ISSUE_SHORT, ISSUE_STYLES, SOURCE_STYLES, STATUS_STYLES, formatTimestamp,
  type AuditLabels, type IssueCount,
} from './chat-audit-shared';

type StepStatus = 'failed' | 'succeeded' | 'running';

interface ToolCallRow {
  chat_id: string;
  incident_id: string | null;
  message_index: number | null;
  tool_call_id: string | null;
  tool_name: string;
  input: string;
  status: StepStatus;
  category: string | null;
  reason: string;
  started_at: string | null;
  chat_title: string;
  source: string | null;
  user: { id: string | null; email: string | null; name: string | null };
}

interface Facet { value: string; count: number; email?: string | null; name?: string | null }

export interface ToolCallsResponse {
  tool_calls: ToolCallRow[];
  total: number;
  page: number;
  total_pages: number;
  search_capped: boolean;
  status_counts: Record<StepStatus | 'total', number>;
  issue_counts: IssueCount[];
  tools: Facet[];
  sources: Facet[];
  users: Facet[];
  labels: AuditLabels;
}

const PER_PAGE = 25;

const STEP_STATUS: { value: StepStatus; label: string; style: string }[] = [
  { value: 'failed', label: 'Failed', style: `bg-red-500/10 ${STATUS_STYLES.failed}` },
  { value: 'succeeded', label: 'Succeeded', style: `bg-zinc-500/10 ${STATUS_STYLES.completed}` },
  { value: 'running', label: 'Running', style: `bg-sky-500/10 ${STATUS_STYLES.in_progress}` },
];

/**
 * Single-select issue chips (they describe failures): exactly one is always
 * checked ("All" by default; choosing a category also filters to failed calls).
 * A radiogroup with roving focus: arrow keys, Home and End move the selection,
 * and Tab enters and leaves the group as one stop. Empty categories are dimmed,
 * never hidden, so the row keeps its shape.
 */
function IssueChips({ counts, labels, value, onChange, loading }: {
  counts: IssueCount[] | undefined; labels: AuditLabels | undefined;
  value: string; onChange: (v: string) => void; loading: boolean;
}) {
  const byCategory = new Map((counts ?? []).map(c => [c.category, c.calls]));
  const total = counts?.reduce((n, c) => n + c.calls, 0);
  const chips = [
    { value: '', label: 'All', full: 'All failures', count: total },
    ...ISSUE_ORDER.map(cat => ({
      value: cat as string, label: ISSUE_SHORT[cat], full: labels?.issues[cat] ?? ISSUE_SHORT[cat],
      count: counts ? byCategory.get(cat) ?? 0 : undefined,
    })),
  ];
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const selected = Math.max(0, chips.findIndex(c => c.value === value));
  const move = (i: number) => {
    const next = (i + chips.length) % chips.length;
    onChange(chips[next].value);
    refs.current[next]?.focus();
  };
  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    const keys: Record<string, () => void> = {
      ArrowRight: () => move(i + 1), ArrowDown: () => move(i + 1),
      ArrowLeft: () => move(i - 1), ArrowUp: () => move(i - 1),
      Home: () => move(0), End: () => move(chips.length - 1),
      Enter: () => onChange(chips[i].value), ' ': () => onChange(chips[i].value),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  };

  return (
    <div role="radiogroup" aria-label="Filter failures by issue" aria-busy={loading}
      className="flex flex-wrap items-center gap-1.5 mb-3">
      {chips.map((c, i) => {
        const checked = i === selected;
        const empty = c.count === 0;
        return (
          <button
            key={c.value || 'all'}
            ref={el => { refs.current[i] = el; }}
            role="radio"
            aria-checked={checked}
            aria-label={`${c.full}: ${c.count ?? 'loading'}`}
            title={c.full}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(c.value)}
            onKeyDown={e => onKeyDown(e, i)}
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 ${
              checked
                ? 'border-zinc-400 bg-zinc-700/70 text-zinc-100'
                : `border-zinc-800 bg-zinc-900/60 hover:border-zinc-600 ${empty ? 'text-zinc-600' : 'text-zinc-300'}`
            }`}
          >
            {c.value && <span className={`h-1.5 w-1.5 rounded-full ${empty ? 'bg-zinc-700' : ISSUE_STYLES[c.value]?.dot}`} />}
            {c.label}
            <span className={`tabular-nums ${loading ? 'opacity-50' : ''} ${checked ? 'text-zinc-300' : 'text-zinc-500'}`}>
              {c.count ?? '—'}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export default function ToolCallsView({ period, onOpen }: {
  period: Period; onOpen: (chatId: string, messageIndex: number | null) => void;
}) {
  const [status, setStatus] = useState<'' | StepStatus>('');
  const [issue, setIssueState] = useState('');
  // An issue describes a failure, so choosing one also filters to failed calls;
  // choosing another status clears it.
  const setIssue = (v: string) => { setIssueState(v); if (v) setStatus('failed'); };
  const changeStatus = (v: '' | StepStatus) => { setStatus(v); if (v !== 'failed') setIssueState(''); };
  const [source, setSource] = useState('');
  const [user, setUser] = useState('');
  const [tool, setTool] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const q = useDebounced(search.trim(), 350);

  useEffect(() => { setPage(1); }, [period, status, issue, source, user, tool, q]);

  const params = new URLSearchParams({ period, page: String(page), per_page: String(PER_PAGE) });
  if (status) params.set('status', status);
  if (issue) params.set('issue', issue);
  if (source) params.set('source', source);
  if (user) params.set('user', user);
  if (tool) params.set('tool', tool);
  if (q) params.set('q', q);

  const { data, error, isLoading } = useQuery<ToolCallsResponse>(
    `/api/monitor/tool-calls?${params.toString()}`, auditFetcher, { staleTime: 15_000 },
  );
  const { view, refreshing } = useKeptData(data, isLoading);
  const filterError = filterErrorOf(error);
  const labels = view?.labels;
  const hasFilters = status || issue || source || user || tool || search;
  const capNote = view?.search_capped
    ? 'Search covers the 500 most recent tool calls matching the other filters; narrow them to reach older ones.' : '';
  const liveText = isLoading ? 'Loading tool calls…'
    : filterError ? '' /* already announced by its role=alert */ : (error ? 'Could not load tool calls.'
      : view ? `${view.total} ${view.total === 1 ? 'tool call' : 'tool calls'} found.` : '');
  const counts = view?.status_counts;

  return (
    <ChartPanel
      title="Tool calls"
      subtitle="Every tool call Aurora made, newest first. Click a row to open the chat at that call. Secrets are redacted."
      loading={isLoading}
    >
      <IssueChips counts={view?.issue_counts} labels={labels} value={issue} onChange={setIssue} loading={refreshing} />

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select aria-label="Status" value={status} onChange={e => changeStatus(e.target.value as '' | StepStatus)} className={selectCls}>
          <option value="">All statuses{counts ? ` (${counts.total})` : ''}</option>
          {STEP_STATUS.map(s => (
            <option key={s.value} value={s.value}>{s.label}{counts ? ` (${counts[s.value]})` : ''}</option>
          ))}
        </select>
        <select aria-label="Source" value={source} onChange={e => setSource(e.target.value)} className={selectCls}>
          <option value="">All sources</option>
          {(view?.sources ?? []).map(s => (
            <option key={s.value} value={s.value}>{labels?.sources[s.value] ?? s.value} ({s.count})</option>
          ))}
        </select>
        <UserFilter value={user} onChange={setUser}
          users={(view?.users ?? []).map(u => ({ value: u.email || u.value, label: `${u.name || u.email || u.value} (${u.count})` }))} />
        <select aria-label="Tool" value={tool} onChange={e => setTool(e.target.value)} className={selectCls}>
          <option value="">All tools</option>
          {(view?.tools ?? []).map(t => <option key={t.value} value={t.value}>{t.value} ({t.count})</option>)}
        </select>
        <SearchBox value={search} onChange={setSearch} label="Search tool, command and reason"
          placeholder='Search tool, command or reason (e.g. "forbidden")' />
        {hasFilters && (
          <button
            onClick={() => { setStatus(''); setIssueState(''); setSource(''); setUser(''); setTool(''); setSearch(''); }}
            className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
          >
            <X className="h-3 w-3" /> Clear
          </button>
        )}
      </div>

      <FilterError message={filterError} />
      <LiveStatus text={liveText} />

      {/* A rejected filter shows only its message, not the last results as if they matched. */}
      {filterError ? null : error && !view ? (
        <EmptyState icon={AlertTriangle} message="Could not load tool calls" hint="Only organization admins can view this." />
      ) : !view || view.tool_calls.length === 0 ? (
        isLoading
          ? <div className="h-40 animate-pulse bg-zinc-800/30 rounded-lg" />
          : <EmptyState icon={Wrench} message={hasFilters ? 'No tool calls match these filters' : 'No tool calls in this period'}
              hint={capNote || undefined} />
      ) : (
        <>
          {view.search_capped && (
            <p className="text-[11px] text-zinc-500 mb-2">{capNote}</p>
          )}
          <div className={`overflow-x-auto rounded-lg border border-zinc-800/60 transition-opacity ${refreshing ? 'opacity-60' : ''}`} aria-busy={refreshing}>
            <table className="w-full text-sm table-fixed min-w-[1000px]">
              <colgroup>
                <col className="w-[112px]" /><col className="w-[22%]" /><col className="w-[96px]" /><col className="w-[110px]" />
                <col /><col className="w-[124px]" /><col className="w-[108px]" /><col className="w-[16%]" />
              </colgroup>
              <thead>
                <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                  {['Time', 'Tool', 'Status', 'Issue', 'Reason', 'User', 'Source', 'Chat'].map(h => (
                    <th key={h} className="text-left px-3 py-2.5 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {view.tool_calls.map((f, i) => {
                  const open = () => onOpen(f.chat_id, f.message_index);
                  return (
                    <tr
                      key={f.tool_call_id ?? `${f.chat_id}-${i}`}
                      onClick={open}
                      onKeyDown={e => {
                        // Only keys pressed on the row itself: Enter on the Incident link follows the link.
                        if (e.target !== e.currentTarget) return;
                        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
                      }}
                      tabIndex={0}
                      className="border-b border-zinc-800/40 hover:bg-zinc-800/20 cursor-pointer transition-colors align-top focus:outline-none focus-visible:bg-zinc-800/30"
                    >
                      <td className="px-3 py-2.5 text-xs text-zinc-500 whitespace-nowrap tabular-nums">{formatTimestamp(f.started_at)}</td>
                      <td className="px-3 py-2.5 min-w-0">
                        <div className="font-mono text-xs text-zinc-200 truncate" title={f.tool_name}>{f.tool_name}</div>
                        {f.input && <div className="font-mono text-[11px] text-zinc-500 truncate mt-0.5" title={f.input}>{f.input}</div>}
                      </td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={f.status} />
                      </td>
                      <td className="px-3 py-2.5">
                        {f.category ? (
                          <span title={labels?.issues[f.category]}
                            className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${ISSUE_STYLES[f.category]?.badge ?? ISSUE_STYLES.other.badge}`}>
                            {ISSUE_SHORT[f.category] ?? f.category}
                          </span>
                        ) : <span className="text-xs text-zinc-600">—</span>}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-red-300/90 min-w-0">
                        {f.status === 'failed' && (
                          <div className="line-clamp-2 break-words" title={f.reason}>{f.reason || 'Failed (no error text)'}</div>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-zinc-400 truncate" title={f.user.email ?? undefined}>
                        {f.user.name || f.user.email || '—'}
                      </td>
                      <td className="px-3 py-2.5">
                        {f.source && (
                          <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap ${SOURCE_STYLES[f.source] ?? SOURCE_STYLES.background}`}>
                            {labels?.sources[f.source] ?? f.source}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-xs min-w-0">
                        <div className="flex items-center gap-1 text-zinc-300">
                          <span className="truncate" title={f.chat_title}>{f.chat_title}</span>
                          <ExternalLink className="h-3 w-3 shrink-0 text-zinc-500" aria-hidden />
                        </div>
                        {f.incident_id && (
                          <Link href={`/incidents/${encodeURIComponent(f.incident_id)}`} onClick={e => e.stopPropagation()}
                            className="text-[11px] text-zinc-500 hover:text-zinc-300">
                            Incident
                          </Link>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager total={view.total} page={page} totalPages={view.total_pages}
            noun={['tool call', 'tool calls']} onPage={setPage} />
        </>
      )}
    </ChartPanel>
  );
}

function StatusBadge({ status }: { status: StepStatus }) {
  const s = STEP_STATUS.find(x => x.value === status);
  return (
    <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${s?.style ?? 'text-zinc-400'}`}>
      {s?.label ?? status}
    </span>
  );
}
