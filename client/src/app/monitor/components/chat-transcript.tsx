'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { AlertTriangle, ArrowLeft, ExternalLink, Eye } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import type { Message } from '@/app/chat/types';
import { MessageItem } from '@/components/chat/message-item';
import { ChartPanel, EmptyState } from './charts';
import { ISSUE_STYLES, SOURCE_STYLES, STATUS_STYLES, formatTimestamp, type AuditLabels } from './chat-audit-shared';

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
    title: string;
    user: { name: string | null; email: string | null };
    source: string;
    trigger_source: string | null;
    status: string;
    created_at: string | null;
    updated_at: string | null;
    incident_id: string | null;
    message_count: number;
    truncated: boolean;
  };
  messages: Message[];
  issues: TranscriptIssue[];
  labels: AuditLabels;
}

/**
 * Read-only view of another user's chat, rendered with the chat's own
 * MessageItem. Read-only because no socket is passed: without `sendRaw`, the
 * tool widgets' approval buttons can't send anything, and `onUpdateMessage`
 * here only changes local display state (expanding a tool call).
 */
export default function ChatTranscript({
  sessionId, focusMessage, onBack,
}: { sessionId: string; focusMessage: number | null; onBack: () => void }) {
  const { data, error } = useQuery<TranscriptResponse>(
    `/api/monitor/chats/${encodeURIComponent(sessionId)}`, jsonFetcher, { staleTime: 30_000 },
  );
  const [messages, setMessages] = useState<Message[]>([]);
  const [highlight, setHighlight] = useState<number | null>(focusMessage);

  const scrollTo = useCallback((index: number | null) => {
    if (index === null) return;
    setHighlight(index);
    // Open the failed tool calls in the message being jumped to.
    const failing = new Set(data?.issues.filter(i => i.message_index === index).map(i => i.tool_call_id));
    setMessages(ms => ms.map(m => m.id !== index ? m : {
      ...m, toolCalls: m.toolCalls?.map(tc => failing.has(tc.id) ? { ...tc, isExpanded: true } : tc),
    }));
    setTimeout(() => document.getElementById(`audit-msg-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 50);
  }, [data]);

  useEffect(() => {
    if (!data) return;
    setMessages(data.messages);
    if (focusMessage !== null) scrollTo(focusMessage);
  }, [data, focusMessage, scrollTo]);

  const updateMessage = useCallback((id: number, updater: (m: Message) => Message) => {
    setMessages(ms => ms.map(m => (m.id === id ? updater(m) : m)));
  }, []);

  const back = (
    <button onClick={onBack} className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200 transition-colors">
      <ArrowLeft className="h-3.5 w-3.5" /> Back
    </button>
  );
  if (!data) {
    return (
      <div className="space-y-4">
        {back}
        {error
          ? <ChartPanel title="Conversation"><EmptyState icon={AlertTriangle} message="Could not load this conversation" /></ChartPanel>
          : <div className="h-64 animate-pulse bg-zinc-900/60 border border-zinc-800/80 rounded-xl" />}
      </div>
    );
  }

  const { chat, issues, labels } = data;
  return (
    <div className="space-y-4">
      {back}
      <div className="grid grid-cols-1 xl:grid-cols-[1fr_320px] gap-6 items-start">
        <ChartPanel title={chat.title} subtitle={`${chat.message_count} messages`} className="min-w-0">
          <div className="flex items-center gap-2 mb-4 px-3 py-2 rounded-md bg-zinc-800/40 border border-zinc-700/40 text-xs text-zinc-400">
            <Eye className="h-3.5 w-3.5 shrink-0" />
            Read-only view of another user&apos;s conversation. Secrets are redacted.
            {chat.truncated && ` Showing the first ${messages.length} messages.`}
          </div>
          <div className="space-y-1">
            {messages.map((m, i) => (
              <div key={m.id} id={`audit-msg-${m.id}`} className={`rounded-lg ${highlight === m.id ? 'ring-1 ring-zinc-400/60' : ''}`}>
                <MessageItem message={m} onUpdateMessage={updateMessage} allMessages={messages} messageIndex={i} />
              </div>
            ))}
          </div>
        </ChartPanel>

        <div className="space-y-4 xl:sticky xl:top-4">
          <ChartPanel title="Details">
            <dl className="grid grid-cols-[90px_1fr] gap-y-1.5 text-xs">
              <dt className="text-zinc-500">User</dt>
              <dd className="text-zinc-300 truncate">{chat.user.name || chat.user.email || '—'}</dd>
              <dt className="text-zinc-500">Source</dt>
              <dd>
                <span className={`px-2 py-0.5 rounded-full font-medium ${SOURCE_STYLES[chat.source] ?? SOURCE_STYLES.background}`}>
                  {labels.sources[chat.source] ?? chat.source}
                </span>
                {chat.trigger_source && <span className="text-zinc-500 ml-1.5">({chat.trigger_source})</span>}
              </dd>
              <dt className="text-zinc-500">Status</dt>
              <dd className={STATUS_STYLES[chat.status]}>{labels.statuses[chat.status] ?? chat.status}</dd>
              <dt className="text-zinc-500">Started</dt>
              <dd className="text-zinc-400">{formatTimestamp(chat.created_at)}</dd>
              <dt className="text-zinc-500">Updated</dt>
              <dd className="text-zinc-400">{formatTimestamp(chat.updated_at)}</dd>
            </dl>
            {chat.incident_id && (
              <Link href={`/incidents/${encodeURIComponent(chat.incident_id)}`} className="inline-flex items-center gap-1.5 mt-3 text-xs text-zinc-300 hover:text-zinc-100">
                Open incident <ExternalLink className="h-3 w-3" />
              </Link>
            )}
          </ChartPanel>

          <ChartPanel title="Issues found" subtitle={issues.length ? 'Click to jump to the message' : undefined}>
            {issues.length === 0 ? (
              <p className="text-xs text-zinc-500">No failed tool calls or reported problems.</p>
            ) : (
              <ul className="space-y-1.5">
                {issues.map((iss, i) => (
                  <li key={i}>
                    <button
                      onClick={() => scrollTo(iss.message_index)}
                      disabled={iss.message_index === null}
                      className="w-full text-left rounded-md px-2 py-1.5 hover:bg-zinc-800/50 disabled:hover:bg-transparent disabled:cursor-default transition-colors"
                    >
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${ISSUE_STYLES[iss.category]?.badge ?? ISSUE_STYLES.other.badge}`}>
                          {labels.issues[iss.category] ?? iss.category}
                        </span>
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
        </div>
      </div>
    </div>
  );
}
