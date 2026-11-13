'use client';

import { useEffect, useState } from 'react';
import { AlertTriangle, MessagesSquare, X } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ChartPanel, EmptyState, type Period } from './charts';
import ChatTranscript from './chat-transcript';
import ToolCallsView, { type ToolCallsResponse } from './tool-calls-view';
import { FilterError, LiveStatus, Pager, SearchBox, UserFilter, auditFetcher, filterErrorOf, selectCls, useDebounced, useKeptData, useUrlParam } from './audit-controls';
import {
  ISSUE_SHORT, ISSUE_STYLES, SOURCE_STYLES, STATUS_STYLES, formatTimestamp, type AuditLabels,
} from './chat-audit-shared';

interface ChatRow {
  id: string;
  title: string;
  user: { id: string | null; email: string | null; name: string | null };
  source: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
  issues: Record<string, number>;
  failed_tool_calls: number;
  match: { message_index: number; sender: 'user' | 'aurora'; text: string } | null;
}

interface ChatsResponse {
  chats: ChatRow[];
  total: number;
  search_capped?: boolean;
  page: number;
  total_pages: number;
  sources: { source: string; label: string; count: number }[];
  users: { id: string; email: string | null; name: string | null; count: number }[];
  labels: AuditLabels;
}

type View = 'conversations' | 'tool-calls';
const PER_PAGE = 25;

/**
 * Admin audit of every chat in the org, in two views: the chat history
 * itself, and every tool call Aurora made in them. The view is kept in the
 * URL (?view=tool-calls) so a link opens the right one.
 */
export default function ChatsTab({ period }: { period: Period }) {
  const [view, setView, viewReady] = useUrlParam('view', 'conversations');
  const [open, setOpen] = useState<{ id: string; messageIndex: number | null } | null>(null);

  // Tool-call total for the period, for the toggle label (one row is enough).
  const { data: callTotals } = useQuery<ToolCallsResponse>(
    `/api/monitor/tool-calls?period=${period}&per_page=1`, jsonFetcher, { staleTime: 15_000 },
  );
  const callCount = callTotals?.status_counts.total;

  const openChat = (id: string, messageIndex: number | null) => setOpen({ id, messageIndex });
  const current: View = view === 'tool-calls' ? 'tool-calls' : 'conversations';

  return (
    <>
      {open && <ChatTranscript sessionId={open.id} focusMessage={open.messageIndex} onBack={() => setOpen(null)} />}
      {/* Kept mounted (hidden) while a transcript is open, so filters and page survive "back". */}
      <div className={`space-y-4 ${open ? 'hidden' : ''}`}>
        {/* Wait for the URL so a ?view=tool-calls link doesn't show or load Chat history first. */}
        {viewReady
          ? <ViewToggle value={current} onChange={setView} callCount={callCount} />
          : <div className="h-[34px]" aria-hidden />}
        {viewReady && (current === 'tool-calls'
          ? <ToolCallsView period={period} onOpen={openChat} />
          : <ChatHistoryView period={period} onOpen={openChat} />)}
      </div>
    </>
  );
}

function ViewToggle({ value, onChange, callCount }: {
  value: View; onChange: (v: View) => void; callCount: number | undefined;
}) {
  const options: { value: View; label: string }[] = [
    { value: 'conversations', label: 'Chat history' },
    { value: 'tool-calls', label: `Tool calls${callCount === undefined ? '' : ` (${callCount})`}` },
  ];
  return (
    <div role="group" aria-label="Audit view" className="inline-flex rounded-lg border border-zinc-800 p-0.5 bg-zinc-900/60">
      {options.map(o => (
        <button
          key={o.value}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
            value === o.value ? 'bg-zinc-700/80 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function ChatHistoryView({ period, onOpen }: {
  period: Period; onOpen: (chatId: string, messageIndex: number | null) => void;
}) {
  const [source, setSource] = useState('');
  const [status, setStatus] = useState('');
  const [user, setUser] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const q = useDebounced(search.trim(), 350);

  useEffect(() => { setPage(1); }, [period, source, status, user, q]);

  const params = new URLSearchParams({ period, page: String(page), per_page: String(PER_PAGE) });
  if (source) params.set('source', source);
  if (status) params.set('status', status);
  if (user) params.set('user', user);
  if (q) params.set('q', q);

  const { data, error, isLoading } = useQuery<ChatsResponse>(
    `/api/monitor/chats?${params.toString()}`, auditFetcher, { staleTime: 15_000 },
  );
  const { view, refreshing } = useKeptData(data, isLoading);
  const filterError = filterErrorOf(error);
  const labels = view?.labels;
  const hasFilters = source || status || user || search;
  const capNote = view?.search_capped ? 'Search covers the 1,000 most recent matching chats; narrow the filters to reach older ones.' : '';
  const liveText = isLoading ? 'Loading chat history…'
    : filterError ? '' /* already announced by its role=alert */ : (error ? 'Could not load chat history.'
      : view ? `${view.total} ${view.total === 1 ? 'chat' : 'chats'} found.` : '');

  return (
    <ChartPanel
      title="Chat history"
      subtitle="Every chat in your organization, from every user and channel. Read-only; secrets are redacted."
      loading={isLoading}
    >
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select aria-label="Source" value={source} onChange={e => setSource(e.target.value)} className={selectCls}>
          <option value="">All sources</option>
          {(view?.sources ?? []).map(s => <option key={s.source} value={s.source}>{s.label} ({s.count})</option>)}
        </select>
        <select aria-label="Status" value={status} onChange={e => setStatus(e.target.value)} className={selectCls}>
          <option value="">Any status</option>
          {Object.entries(labels?.statuses ?? {}).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <UserFilter value={user} onChange={setUser}
          users={(view?.users ?? []).map(u => ({ value: u.email || u.id, label: `${u.name || u.email || u.id} (${u.count})` }))} />
        <SearchBox value={search} onChange={setSearch} label="Search questions and replies"
          placeholder='Search questions and replies (e.g. "permission denied")' />
        {hasFilters && (
          <button
            onClick={() => { setSource(''); setStatus(''); setUser(''); setSearch(''); }}
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
        <EmptyState icon={AlertTriangle} message="Could not load chat history" hint="Only organization admins can view all chats." />
      ) : !view || view.chats.length === 0 ? (
        isLoading
          ? <div className="h-40 animate-pulse bg-zinc-800/30 rounded-lg" />
          : <EmptyState icon={MessagesSquare} message="No chats match these filters" hint={capNote || undefined} />
      ) : (
        <>
          <div className={`overflow-x-auto rounded-lg border border-zinc-800/60 transition-opacity ${refreshing ? 'opacity-60' : ''}`} aria-busy={refreshing}>
            <table className="w-full text-sm min-w-[900px]">
              <thead>
                <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                  {['Chat', 'User', 'Source', 'Status', 'Issues', 'Started', 'Updated'].map(h => (
                    <th key={h} className="text-left px-3 py-2.5 font-medium">{h}</th>
                  ))}
                  <th className="text-right px-3 py-2.5 font-medium">Msgs</th>
                </tr>
              </thead>
              <tbody>
                {view.chats.map(c => (
                  <ChatRowView key={c.id} chat={c} labels={view.labels}
                    onOpen={() => onOpen(c.id, c.match?.message_index ?? null)} />
                ))}
              </tbody>
            </table>
          </div>
          {capNote && <p className="text-[11px] text-zinc-500 mt-2">{capNote}</p>}
          <Pager total={view.total} page={page} totalPages={view.total_pages}
            noun={['chat', 'chats']} onPage={setPage} />
        </>
      )}
    </ChartPanel>
  );
}

function ChatRowView({ chat: c, labels, onOpen }: { chat: ChatRow; labels: AuditLabels; onOpen: () => void }) {
  const issueEntries = Object.entries(c.issues);
  const otherFailures = c.failed_tool_calls - issueEntries.reduce((n, [, v]) => n + v, 0);
  const cell = 'px-3 py-2.5 text-xs text-zinc-500 whitespace-nowrap tabular-nums';
  return (
    <tr
      onClick={onOpen}
      onKeyDown={e => {
        // Only keys pressed on the row itself; a focused control inside keeps its own behaviour.
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(); }
      }}
      tabIndex={0}
      className="border-b border-zinc-800/40 hover:bg-zinc-800/20 cursor-pointer transition-colors align-top focus:outline-none focus-visible:bg-zinc-800/30"
    >
      <td className="px-3 py-2.5 max-w-[420px]">
        <div className="text-zinc-200 text-sm truncate" title={c.title}>{c.title}</div>
        {c.match && (
          <div className="text-xs text-zinc-500 mt-1 line-clamp-2 break-words">
            <span className="text-zinc-400">{c.match.sender === 'user' ? 'User' : 'Aurora'}: </span>{c.match.text}
          </div>
        )}
      </td>
      <td className="px-3 py-2.5 text-xs text-zinc-400 truncate max-w-[160px]" title={c.user.email ?? undefined}>
        {c.user.name || c.user.email || '—'}
      </td>
      <td className="px-3 py-2.5">
        <span className={`px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${SOURCE_STYLES[c.source] ?? SOURCE_STYLES.background}`}>
          {labels.sources[c.source] ?? c.source}
        </span>
      </td>
      <td className={`px-3 py-2.5 text-xs whitespace-nowrap ${STATUS_STYLES[c.status]}`}>{labels.statuses[c.status] ?? c.status}</td>
      <td className="px-3 py-2.5">
        <div className="flex flex-wrap gap-1">
          {issueEntries.map(([cat, n]) => (
            <span key={cat} title={labels.issues[cat]}
              className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${ISSUE_STYLES[cat]?.badge}`}>
              {ISSUE_SHORT[cat] ?? cat} · {n}
            </span>
          ))}
          {otherFailures > 0 && (
            <span className="px-1.5 py-0.5 rounded text-[11px] text-zinc-400 bg-zinc-800/60 whitespace-nowrap">{otherFailures} failed</span>
          )}
          {!issueEntries.length && otherFailures <= 0 && <span className="text-xs text-zinc-600">—</span>}
        </div>
      </td>
      <td className={cell}>{formatTimestamp(c.created_at)}</td>
      <td className={cell}>{formatTimestamp(c.updated_at)}</td>
      <td className={`${cell} text-right`}>{c.message_count}</td>
    </tr>
  );
}
