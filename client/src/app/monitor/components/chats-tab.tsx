'use client';

import { useEffect, useState } from 'react';
import {
  AlertTriangle, ChevronLeft, ChevronRight, KeyRound, MessagesSquare, PlugZap,
  Search, ShieldBan, TerminalSquare, Timer, X, XCircle,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ChartPanel, EmptyState, type Period } from './charts';
import ChatTranscript from './chat-transcript';
import { ISSUE_STYLES, SOURCE_STYLES, STATUS_STYLES, formatTimestamp, type AuditLabels } from './chat-audit-shared';

interface Failure {
  message_index: number | null;
  tool_call_id: string | null;
  tool_name: string;
  input: string;
  category: string;
  reason: string;
  started_at: string | null;
}

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
  failures: Failure[];
  match: { message_index: number; sender: 'user' | 'aurora'; text: string } | null;
}

interface IssueCount { category: string; label: string; calls: number; chats: number }

interface ChatsResponse {
  chats: ChatRow[];
  total: number;
  page: number;
  total_pages: number;
  issue_counts: IssueCount[];
  sources: { source: string; label: string; count: number }[];
  users: { id: string; email: string | null; name: string | null; count: number }[];
  labels: AuditLabels;
}

const ISSUE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  missing_permission: KeyRound,
  blocked_command: ShieldBan,
  not_connected: PlugZap,
  timeout: Timer,
  unavailable: TerminalSquare,
  other: XCircle,
};

const PER_PAGE = 25;
const selectCls = 'bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2 py-1 text-xs text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-600 max-w-[180px]';

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

export default function ChatsTab({ period }: { period: Period }) {
  const [source, setSource] = useState('');
  const [status, setStatus] = useState('');
  const [issue, setIssue] = useState('');
  const [user, setUser] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<{ id: string; messageIndex: number | null } | null>(null);
  const q = useDebounced(search.trim(), 350);

  useEffect(() => { setPage(1); }, [period, source, status, issue, user, q]);

  const params = new URLSearchParams({ period, page: String(page), per_page: String(PER_PAGE) });
  if (source) params.set('source', source);
  if (status) params.set('status', status);
  if (issue) params.set('issue', issue);
  if (user) params.set('user', user);
  if (q) params.set('q', q);

  const { data, error, isLoading } = useQuery<ChatsResponse>(
    `/api/monitor/chats?${params.toString()}`, jsonFetcher, { staleTime: 15_000 },
  );

  if (open) {
    return <ChatTranscript sessionId={open.id} focusMessage={open.messageIndex} onBack={() => setOpen(null)} />;
  }

  const labels = data?.labels;
  const hasFilters = source || status || issue || user || search;

  return (
    <div className="space-y-6">
      {/* Failed tool calls per issue type in the period; click to filter */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        {(data?.issue_counts ?? []).map(c => {
          const Icon = ISSUE_ICONS[c.category] ?? AlertTriangle;
          const active = issue === c.category;
          return (
            <button
              key={c.category}
              onClick={() => setIssue(active ? '' : c.category)}
              aria-pressed={active}
              className={`text-left bg-zinc-900/60 border rounded-xl p-3.5 transition-all duration-200 ${
                active ? 'border-zinc-500 ring-1 ring-zinc-500/40' : 'border-zinc-800/80 hover:border-zinc-700'
              }`}
            >
              <div className="flex items-center gap-1.5 mb-1.5">
                <Icon className={`h-3.5 w-3.5 ${c.calls ? ISSUE_STYLES[c.category]?.text : 'text-zinc-600'}`} />
                <span className="text-[11px] text-zinc-500 font-medium uppercase tracking-wider truncate">{c.label}</span>
              </div>
              <p className="text-2xl font-semibold text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>{c.calls}</p>
              <p className="text-[11px] text-zinc-500 mt-0.5">failed calls in {c.chats} {c.chats === 1 ? 'chat' : 'chats'}</p>
            </button>
          );
        })}
      </div>

      <ChartPanel
        title="Conversations"
        subtitle="Every chat in your organization, from every user and channel. Read-only; secrets are redacted."
        loading={isLoading}
      >
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <select aria-label="Source" value={source} onChange={e => setSource(e.target.value)} className={selectCls}>
            <option value="">All sources</option>
            {(data?.sources ?? []).map(s => <option key={s.source} value={s.source}>{s.label} ({s.count})</option>)}
          </select>
          <select aria-label="Status" value={status} onChange={e => setStatus(e.target.value)} className={selectCls}>
            <option value="">Any status</option>
            {Object.entries(labels?.statuses ?? {}).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
          <select aria-label="Issue" value={issue} onChange={e => setIssue(e.target.value)} className={selectCls}>
            <option value="">All chats</option>
            <option value="any">Any failed tool call</option>
            {Object.entries(labels?.issues ?? {}).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
          <select aria-label="User" value={user} onChange={e => setUser(e.target.value)} className={selectCls}>
            <option value="">All users</option>
            {(data?.users ?? []).map(u => <option key={u.id} value={u.id}>{u.name || u.email || u.id} ({u.count})</option>)}
          </select>
          <div className="relative flex-1 min-w-[200px]">
            <Search className="h-3.5 w-3.5 text-zinc-500 absolute left-2 top-1/2 -translate-y-1/2" />
            <input
              type="search"
              value={search}
              onChange={e => setSearch(e.target.value)}
              maxLength={200}
              placeholder='Search questions and replies (e.g. "permission denied")'
              aria-label="Search questions and replies"
              className="w-full bg-zinc-800/50 border border-zinc-700/50 rounded-md pl-7 pr-2.5 py-1 text-xs text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-600"
            />
          </div>
          {hasFilters && (
            <button
              onClick={() => { setSource(''); setStatus(''); setIssue(''); setUser(''); setSearch(''); }}
              className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
            >
              <X className="h-3 w-3" /> Clear
            </button>
          )}
        </div>

        {error && !data ? (
          <EmptyState icon={AlertTriangle} message="Could not load conversations" hint="Only organization admins can view all chats." />
        ) : !data || data.chats.length === 0 ? (
          isLoading
            ? <div className="h-40 animate-pulse bg-zinc-800/30 rounded-lg" />
            : <EmptyState icon={MessagesSquare} message="No conversations match these filters" />
        ) : (
          <>
            <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
              <table className="w-full text-sm min-w-[900px]">
                <thead>
                  <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                    <th className="text-left px-3 py-2.5 font-medium">Conversation</th>
                    <th className="text-left px-3 py-2.5 font-medium">User</th>
                    <th className="text-left px-3 py-2.5 font-medium">Source</th>
                    <th className="text-left px-3 py-2.5 font-medium">Status</th>
                    <th className="text-left px-3 py-2.5 font-medium">Issues</th>
                    <th className="text-left px-3 py-2.5 font-medium">Started</th>
                    <th className="text-left px-3 py-2.5 font-medium">Updated</th>
                    <th className="text-right px-3 py-2.5 font-medium">Msgs</th>
                  </tr>
                </thead>
                <tbody>
                  {data.chats.map(c => (
                    <ChatRowView key={c.id} chat={c} labels={data.labels} showFailures={!!issue}
                      onOpen={(messageIndex) => setOpen({ id: c.id, messageIndex })} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between mt-4">
              <p className="text-xs text-zinc-500">
                {data.total.toLocaleString()} {data.total === 1 ? 'conversation' : 'conversations'} — page {data.page} of {data.total_pages}
              </p>
              {data.total_pages > 1 && (
                <div className="flex items-center gap-1">
                  <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1} aria-label="Previous page"
                    className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed">
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                  <button onClick={() => setPage(p => Math.min(data.total_pages, p + 1))} disabled={page >= data.total_pages} aria-label="Next page"
                    className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed">
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </ChartPanel>
    </div>
  );
}

function ChatRowView({
  chat: c, labels, showFailures, onOpen,
}: { chat: ChatRow; labels: AuditLabels; showFailures: boolean; onOpen: (messageIndex: number | null) => void }) {
  const issueEntries = Object.entries(c.issues);
  const otherFailures = c.failed_tool_calls - issueEntries.reduce((n, [, v]) => n + v, 0);
  const openRow = () => onOpen(c.match?.message_index ?? c.failures[0]?.message_index ?? null);
  const cell = 'px-3 py-2.5 text-xs text-zinc-500 whitespace-nowrap';
  return (
    <tr
      onClick={openRow}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRow(); } }}
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
        {showFailures && c.failures.length > 0 && (
          <ul className="mt-1.5 space-y-1">
            {c.failures.map((f, i) => (
              <li key={f.tool_call_id ?? i}>
                <button
                  onClick={e => { e.stopPropagation(); onOpen(f.message_index); }}
                  className="w-full text-left text-xs rounded px-1.5 py-1 hover:bg-zinc-800/60"
                  title={f.input}
                >
                  <span className="font-mono text-zinc-300">{f.tool_name}</span>
                  <span className="text-red-300/90"> — {f.reason || 'failed'}</span>
                </button>
              </li>
            ))}
          </ul>
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
            <span key={cat} className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${ISSUE_STYLES[cat]?.badge}`}>
              {labels.issues[cat]} · {n}
            </span>
          ))}
          {otherFailures > 0 && (
            <span className="px-1.5 py-0.5 rounded text-[11px] text-zinc-400 bg-zinc-800/60 whitespace-nowrap">{otherFailures} failed</span>
          )}
          {!issueEntries.length && otherFailures <= 0 && <span className="text-xs text-zinc-600">—</span>}
        </div>
      </td>
      <td className={cell} style={{ fontVariantNumeric: 'tabular-nums' }}>{formatTimestamp(c.created_at)}</td>
      <td className={cell} style={{ fontVariantNumeric: 'tabular-nums' }}>{formatTimestamp(c.updated_at)}</td>
      <td className={`${cell} text-right`} style={{ fontVariantNumeric: 'tabular-nums' }}>{c.message_count}</td>
    </tr>
  );
}
