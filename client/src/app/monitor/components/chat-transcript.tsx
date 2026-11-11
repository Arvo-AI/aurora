'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  AlertTriangle, ArrowLeft, Bot, ChevronDown, Eye, ExternalLink, User, Wrench,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ChartPanel, EmptyState } from './charts';
import { ISSUE_STYLES, SOURCE_STYLES, STATUS_STYLES, formatTimestamp, safeHref } from './chat-audit-shared';

interface TranscriptToolCall {
  id: string | null;
  tool_name: string;
  input: string;
  output: string;
  status: string;
  issue: string | null;
  reason: string;
}

interface TranscriptMessage {
  index: number;
  sender: 'user' | 'aurora';
  text: string;
  timestamp: string | null;
  tool_calls: TranscriptToolCall[];
  issue: string | null;
}

interface TranscriptIssue {
  category: string;
  message_index: number | null;
  tool_call_id: string | null;
  tool_name: string | null;
  summary: string;
  from_reply?: boolean;
}

interface TranscriptResponse {
  chat: {
    id: string;
    title: string;
    user: { id: string | null; email: string | null; name: string | null };
    source: string;
    trigger_source: string | null;
    status: string;
    created_at: string | null;
    updated_at: string | null;
    incident_id: string | null;
    message_count: number;
    truncated: boolean;
  };
  messages: TranscriptMessage[];
  issues: TranscriptIssue[];
  other_failed_tool_calls: { tool_name: string; input: string; category: string; reason: string; started_at: string | null }[];
  labels: { sources: Record<string, string>; issues: Record<string, string>; statuses: Record<string, string> };
}

function IssueBadge({ category, labels }: { category: string; labels: Record<string, string> }) {
  return (
    <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${ISSUE_STYLES[category]?.badge ?? ISSUE_STYLES.other.badge}`}>
      {labels[category] ?? category}
    </span>
  );
}

function ToolCallBlock({
  tc, labels, defaultOpen,
}: { tc: TranscriptToolCall; labels: Record<string, string>; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const failed = tc.status === 'failed';
  return (
    <div className={`rounded-md border ${failed ? 'border-red-900/40 bg-red-950/10' : 'border-zinc-800/70 bg-zinc-900/40'}`}>
      <button
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        <Wrench className={`h-3.5 w-3.5 shrink-0 ${failed ? 'text-red-400' : 'text-zinc-500'}`} />
        <span className="font-mono text-xs text-zinc-300 shrink-0">{tc.tool_name}</span>
        <span className="font-mono text-xs text-zinc-500 truncate flex-1">{tc.input}</span>
        {tc.issue && <IssueBadge category={tc.issue} labels={labels} />}
        {failed && !tc.issue && <span className="text-[11px] text-red-400">failed</span>}
        <ChevronDown className={`h-3.5 w-3.5 text-zinc-500 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {tc.reason && <p className="text-xs text-red-300/90">{tc.reason}</p>}
          <div>
            <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Input</p>
            <pre className="text-xs font-mono text-zinc-300 bg-zinc-950/60 border border-zinc-800/60 rounded p-2 whitespace-pre-wrap break-all max-h-40 overflow-auto">{tc.input || '—'}</pre>
          </div>
          <div>
            <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Output</p>
            <pre className="text-xs font-mono text-zinc-400 bg-zinc-950/60 border border-zinc-800/60 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{tc.output || '—'}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ChatTranscript({
  sessionId, focusMessage, onBack,
}: { sessionId: string; focusMessage: number | null; onBack: () => void }) {
  const { data, error, isLoading } = useQuery<TranscriptResponse>(
    `/api/monitor/chats/${encodeURIComponent(sessionId)}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );
  const [highlight, setHighlight] = useState<number | null>(focusMessage);

  const scrollTo = (index: number | null) => {
    if (index === null) return;
    setHighlight(index);
    document.getElementById(`audit-msg-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };

  const loaded = !!data;
  useEffect(() => {
    if (!loaded || focusMessage === null) return;
    const t = setTimeout(() => {
      document.getElementById(`audit-msg-${focusMessage}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 50);
    return () => clearTimeout(t);
  }, [loaded, focusMessage]);

  const labels = data?.labels.issues ?? {};
  const failedCallIds = new Set(data?.issues.filter(i => i.tool_call_id).map(i => i.tool_call_id));
  const incidentHref = data?.chat.incident_id ? safeHref(`/incidents/${data.chat.incident_id}`) : null;

  return (
    <div className="space-y-4">
      <button onClick={onBack} className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200 transition-colors">
        <ArrowLeft className="h-3.5 w-3.5" /> All conversations
      </button>

      {error && !data ? (
        <ChartPanel title="Conversation">
          <EmptyState icon={AlertTriangle} message="Could not load this conversation" />
        </ChartPanel>
      ) : !data ? (
        <div className="h-64 animate-pulse bg-zinc-900/60 border border-zinc-800/80 rounded-xl" />
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-[1fr_320px] gap-6 items-start">
          <ChartPanel title={data.chat.title} subtitle={`${data.chat.message_count} messages`} className="min-w-0">
            <div className="flex items-center gap-2 mb-4 px-3 py-2 rounded-md bg-zinc-800/40 border border-zinc-700/40 text-xs text-zinc-400">
              <Eye className="h-3.5 w-3.5 shrink-0" />
              Read-only view of another user&apos;s conversation. Secrets are redacted.
            </div>
            {data.chat.truncated && (
              <p className="text-xs text-zinc-500 mb-3">Showing the first {data.messages.length} messages.</p>
            )}
            <ol className="space-y-4">
              {data.messages.map(m => {
                const isUser = m.sender === 'user';
                const focused = highlight === m.index;
                return (
                  <li
                    key={m.index}
                    id={`audit-msg-${m.index}`}
                    className={`rounded-lg p-3 transition-shadow ${isUser ? 'bg-zinc-800/40' : 'bg-transparent'} ${
                      focused ? 'ring-1 ring-zinc-400/60' : ''
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1.5 text-xs">
                      {isUser ? <User className="h-3.5 w-3.5 text-zinc-400" /> : <Bot className="h-3.5 w-3.5 text-zinc-400" />}
                      <span className="font-medium text-zinc-300">
                        {isUser ? (data.chat.user.name || data.chat.user.email || 'User') : 'Aurora'}
                      </span>
                      {m.timestamp && <span className="text-zinc-600">{formatTimestamp(m.timestamp)}</span>}
                      {m.issue && <IssueBadge category={m.issue} labels={labels} />}
                    </div>
                    {m.text && (
                      <p className="text-sm text-zinc-300 whitespace-pre-wrap break-words leading-relaxed">{m.text}</p>
                    )}
                    {m.tool_calls.length > 0 && (
                      <div className="mt-2 space-y-1.5">
                        {m.tool_calls.map((tc, i) => (
                          <ToolCallBlock
                            key={tc.id ?? i}
                            tc={tc}
                            labels={labels}
                            defaultOpen={focused && failedCallIds.has(tc.id)}
                          />
                        ))}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          </ChartPanel>

          <div className="space-y-4 xl:sticky xl:top-4">
            <ChartPanel title="Details">
              <dl className="grid grid-cols-[90px_1fr] gap-y-1.5 text-xs">
                <dt className="text-zinc-500">User</dt>
                <dd className="text-zinc-300 truncate">{data.chat.user.name || data.chat.user.email || '—'}</dd>
                <dt className="text-zinc-500">Source</dt>
                <dd>
                  <span className={`px-2 py-0.5 rounded-full font-medium ${SOURCE_STYLES[data.chat.source] ?? SOURCE_STYLES.background}`}>
                    {data.labels.sources[data.chat.source] ?? data.chat.source}
                  </span>
                  {data.chat.trigger_source && <span className="text-zinc-500 ml-1.5">({data.chat.trigger_source})</span>}
                </dd>
                <dt className="text-zinc-500">Status</dt>
                <dd className={STATUS_STYLES[data.chat.status]}>{data.labels.statuses[data.chat.status] ?? data.chat.status}</dd>
                <dt className="text-zinc-500">Started</dt>
                <dd className="text-zinc-400">{formatTimestamp(data.chat.created_at)}</dd>
                <dt className="text-zinc-500">Updated</dt>
                <dd className="text-zinc-400">{formatTimestamp(data.chat.updated_at)}</dd>
              </dl>
              {incidentHref && (
                <Link href={incidentHref} className="inline-flex items-center gap-1.5 mt-3 text-xs text-zinc-300 hover:text-zinc-100">
                  Open incident <ExternalLink className="h-3 w-3" />
                </Link>
              )}
            </ChartPanel>

            <ChartPanel title="Issues found" subtitle={data.issues.length ? 'Click to jump to the message' : undefined}>
              {data.issues.length === 0 ? (
                <p className="text-xs text-zinc-500">No permission, blocked-command or connection issues detected.</p>
              ) : (
                <ul className="space-y-1.5">
                  {data.issues.map((iss, i) => (
                    <li key={i}>
                      <button
                        onClick={() => scrollTo(iss.message_index)}
                        disabled={iss.message_index === null}
                        className="w-full text-left rounded-md px-2 py-1.5 hover:bg-zinc-800/50 disabled:hover:bg-transparent disabled:cursor-default transition-colors"
                      >
                        <div className="flex items-center gap-1.5 mb-0.5">
                          <IssueBadge category={iss.category} labels={labels} />
                          {iss.tool_name && <span className="font-mono text-[11px] text-zinc-400 truncate">{iss.tool_name}</span>}
                          {iss.from_reply && <span className="text-[11px] text-zinc-500">in reply</span>}
                          {iss.message_index === null && <span className="text-[11px] text-zinc-500">sub-agent</span>}
                        </div>
                        <p className="text-xs text-zinc-400 line-clamp-2 break-words">{iss.summary}</p>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </ChartPanel>

            {data.other_failed_tool_calls.length > 0 && (
              <ChartPanel title="Other failed tool calls" subtitle="Recorded for sub-agents, not shown in the transcript">
                <ul className="space-y-2">
                  {data.other_failed_tool_calls.map((f, i) => (
                    <li key={i} className="text-xs">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono text-zinc-300">{f.tool_name}</span>
                        {f.category !== 'other' && <IssueBadge category={f.category} labels={labels} />}
                      </div>
                      <p className="font-mono text-zinc-500 truncate" title={f.input}>{f.input}</p>
                      {f.reason && <p className="text-red-300/80 line-clamp-2 break-words">{f.reason}</p>}
                    </li>
                  ))}
                </ul>
              </ChartPanel>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
