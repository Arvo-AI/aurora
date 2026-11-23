'use client';

import Link from 'next/link';
import { useState } from 'react';
import { Hash, Lock } from 'lucide-react';
import { useQuery } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';
import { Level, LevelDial, LevelPreview, LEVELS } from './slackLevels';

type ReportStatus = 'new' | 'verifying' | 'confirmed' | 'answered' | 'dismissed' | 'merged' | 'skipped';
type ReportKind = 'problem' | 'question';

interface Report {
  id: string;
  channel: string;
  said_at: string;
  kind: ReportKind;
  asset: string | null;
  claim: string;
  status: ReportStatus;
  status_detail: string | null;
  incident_id: string | null;
  reply: string | null;
  helpful: number;
  unhelpful: number;
}

interface Channel {
  channel_id: string;
  name: string;
  is_private: boolean;
  chosen: Level | null;
  level: Level;
  last_read_at: string | null;
}

interface SlackResponse {
  enabled: boolean;
  company_level: Level;
  reports: Report[];
  channels: Channel[];
  can_edit: boolean;
  can_set_company: boolean;
}

const OUTCOME: Record<ReportStatus, string> = {
  new: 'Waiting',
  verifying: 'Looking into it',
  confirmed: 'Real problem',
  answered: 'Answered',
  dismissed: 'Nothing found',
  merged: 'Same as an earlier one',
  skipped: 'Not looked into',
};

const OUTCOME_STYLES: Record<ReportStatus, string> = {
  new: 'bg-zinc-800 text-zinc-400 border-zinc-700/60',
  verifying: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
  confirmed: 'bg-red-500/10 text-red-400 border-red-500/20',
  answered: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  dismissed: 'bg-zinc-900 text-zinc-500 border-zinc-800',
  merged: 'bg-zinc-900 text-zinc-500 border-zinc-800',
  skipped: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
};

const CARD = 'bg-zinc-900/60 border border-zinc-800/80 rounded-xl';

const slackFetcher = async (key: string, signal: AbortSignal): Promise<SlackResponse> => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load Slack settings: ${res.status}`);
  const data = await res.json();
  return {
    enabled: Boolean(data.enabled),
    company_level: data.company_level || 'off',
    reports: data.reports || [],
    channels: data.channels || [],
    can_edit: Boolean(data.can_edit),
    can_set_company: Boolean(data.can_set_company),
  };
};

/** POST a level and report whether it was saved. */
async function saveLevel(url: string, level: Level | null): Promise<boolean> {
  try {
    const res = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

function CompanyLevel({ level, canSet, onChanged }: {
  readonly level: Level;
  readonly canSet: boolean;
  readonly onChanged: () => void;
}) {
  // What the dial shows: the saved level, or the one being looked at before saving.
  const [shown, setShown] = useState<Level | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const current = shown ?? level;

  const choose = async (next: Level) => {
    setShown(next);
    setSaving(true);
    setFailed(false);
    const saved = await saveLevel('/api/detection/chat-level', next);
    setSaving(false);
    setFailed(!saved);
    if (!saved) setShown(null);
    onChanged();
  };

  return (
    <div className={`${CARD} p-5`}>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <p className="text-sm font-medium text-zinc-200">Company level</p>
          <p className="text-xs text-zinc-500 mt-0.5">What Aurora does in a public channel nobody has set.</p>
        </div>
        <LevelDial value={current} onChange={choose} disabled={!canSet || saving} label="Company level" />
      </div>
      <p className="text-sm text-zinc-400 mt-4">{LEVELS.find((l) => l.key === current)?.means}</p>
      <div className="mt-4">
        <LevelPreview level={current} />
      </div>
      {!canSet && <p className="text-xs text-zinc-600 mt-3">Only an admin can change the company level.</p>}
      {failed && <p className="text-xs text-red-400 mt-3">That did not save. Try again.</p>}
    </div>
  );
}

function ChannelRow({ channel, canEdit, onChanged }: {
  readonly channel: Channel;
  readonly canEdit: boolean;
  readonly onChanged: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const url = `/api/detection/chat-channels/${channel.channel_id}`;

  const choose = async (level: Level | null) => {
    setSaving(true);
    setFailed(false);
    const saved = await saveLevel(url, level);
    setSaving(false);
    setFailed(!saved);
    onChanged();
  };

  const inherited = channel.is_private ? 'Private: off until someone sets it' : 'Follows the company level';
  return (
    <div className="flex items-center justify-between gap-4 py-2.5 flex-wrap">
      <div className="min-w-0">
        <p className="text-sm text-zinc-200 flex items-center gap-1.5">
          {channel.is_private
            ? <Lock className="h-3.5 w-3.5 text-zinc-500" aria-label="Private channel" />
            : <Hash className="h-3.5 w-3.5 text-zinc-500" aria-hidden />}
          <span className="truncate">{channel.name}</span>
        </p>
        <p className="text-[11px] text-zinc-600 mt-0.5">
          {channel.chosen === null ? inherited : 'Set for this channel'}
          {channel.chosen !== null && canEdit && (
            <>
              {' · '}
              <button type="button" disabled={saving} onClick={() => choose(null)} className="text-zinc-400 hover:text-zinc-200 underline underline-offset-2">
                {channel.is_private ? 'turn off' : 'follow the company level'}
              </button>
            </>
          )}
          {channel.last_read_at && channel.level !== 'off' && ` · read ${formatTimeAgo(channel.last_read_at)}`}
          {failed && <span className="text-red-400"> · that did not save</span>}
        </p>
      </div>
      <LevelDial
        compact value={channel.level} onChange={choose} disabled={!canEdit || saving}
        label={`Level for ${channel.name}`}
      />
    </div>
  );
}

function votesLine(report: Report): string | null {
  const parts = [];
  if (report.helpful) parts.push(`${report.helpful} found it helpful`);
  if (report.unhelpful) parts.push(`${report.unhelpful} did not`);
  return parts.length ? parts.join(' · ') : null;
}

function ReportRow({ report }: { readonly report: Report }) {
  const ended = !['new', 'verifying'].includes(report.status);
  const outcome = report.kind === 'question' && report.status === 'dismissed' ? 'Nothing to add' : OUTCOME[report.status];
  const votes = votesLine(report);
  return (
    <div className={`${CARD} p-4`}>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-zinc-200">{report.claim}</p>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border whitespace-nowrap ${OUTCOME_STYLES[report.status]}`}>
          {outcome ?? report.status}
        </span>
      </div>
      <div className="flex items-center gap-3 mt-2 flex-wrap text-xs text-zinc-500">
        <span>#{report.channel}</span>
        <span>{formatTimeAgo(report.said_at)}</span>
        <span>{report.kind === 'question' ? 'Question' : 'Problem'}</span>
        {report.asset && <span className="text-zinc-400">{report.asset}</span>}
        {report.incident_id && (
          <Link href={`/incidents/${report.incident_id}`} className="text-sky-400 hover:underline">
            Open the incident
          </Link>
        )}
      </div>
      {report.reply && (
        <div className="mt-3 border-l-2 border-sky-500/60 pl-3">
          <p className="text-xs font-semibold text-sky-400">Aurora said in the thread</p>
          <p className="text-sm text-zinc-300 whitespace-pre-wrap">{report.reply}</p>
          {votes && <p className="text-[11px] text-zinc-500 mt-1">{votes}</p>}
        </div>
      )}
      {!report.reply && ended && <p className="text-xs text-zinc-600 mt-3">Aurora posted nothing in the channel.</p>}
      {report.status_detail && (
        <p className="text-xs text-zinc-500 mt-2 whitespace-pre-wrap">
          <span className="text-zinc-600">What it looked at: </span>{report.status_detail}
        </p>
      )}
    </div>
  );
}

/** How much Aurora does in each Slack channel, and what it noticed and said there.
 *  Renders nothing when the install has it off or the org has no Slack. */
export default function AuroraInSlack() {
  const { data, mutate } = useQuery<SlackResponse>(
    '/api/detection/reports',
    slackFetcher,
    { staleTime: 10_000, refreshInterval: 30_000, retryCount: 0 },
  );
  if (!data?.enabled) return null;

  const reading = data.channels.filter((c) => c.level !== 'off').length;
  return (
    <section className="mt-12" aria-labelledby="aurora-in-slack">
      <h3 id="aurora-in-slack" className="text-lg font-semibold">Aurora in Slack</h3>
      <p className="text-sm text-zinc-500 mt-1 mb-4">
        How much Aurora does in your channels without being asked. Anyone in a channel can also change it from one of Aurora&apos;s replies there.
      </p>

      <CompanyLevel level={data.company_level} canSet={data.can_set_company} onChanged={() => mutate()} />

      {data.channels.length > 0 && (
        <div className={`${CARD} px-5 py-3 mt-4`}>
          <div className="flex items-baseline justify-between">
            <p className="text-[11px] uppercase tracking-wide text-zinc-600 py-1.5">Channels Aurora is in</p>
            <p className="text-[11px] text-zinc-600">
              Reading {reading} of {data.channels.length}
            </p>
          </div>
          <div className="divide-y divide-zinc-800/70">
            {data.channels.map((channel) => (
              <ChannelRow key={channel.channel_id} channel={channel} canEdit={data.can_edit} onChanged={() => mutate()} />
            ))}
          </div>
        </div>
      )}

      <h4 className="text-sm font-medium text-zinc-300 mt-8 mb-1">What Aurora noticed</h4>
      <p className="text-xs text-zinc-500 mb-3">
        One line about what was said, what Aurora found, and what it replied. Kept for 30 days. Messages and their authors are not stored.
      </p>
      {data.reports.length === 0 ? (
        <div className={`${CARD} text-sm text-zinc-500 py-8 text-center`}>
          {reading === 0 ? 'Aurora is not reading any channel' : 'Nothing noticed yet'}
        </div>
      ) : (
        <div className="space-y-2">
          {data.reports.map((report) => <ReportRow key={report.id} report={report} />)}
        </div>
      )}
    </section>
  );
}
