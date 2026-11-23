'use client';

import Link from 'next/link';
import { useState } from 'react';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useQuery } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';

type ReportStatus = 'new' | 'verifying' | 'confirmed' | 'dismissed' | 'merged' | 'skipped';

interface Report {
  id: string;
  channel: string;
  said_at: string;
  asset: string | null;
  claim: string;
  status: ReportStatus;
  status_detail: string | null;
  incident_id: string | null;
  messages: number;
}

interface Channel {
  channel_id: string;
  name: string;
  enabled: boolean;
  last_read_at: string | null;
}

interface ReportsResponse {
  enabled: boolean;
  reports: Report[];
  channels: Channel[];
  can_edit: boolean;
}

const STATUS_LABEL: Record<ReportStatus, string> = {
  new: 'Waiting',
  verifying: 'Looking into it',
  confirmed: 'Real',
  dismissed: 'Found nothing',
  merged: 'Same as an earlier report',
  skipped: 'Not looked into',
};

const STATUS_STYLES: Record<ReportStatus, string> = {
  new: 'bg-zinc-800 text-zinc-400 border-zinc-700/60',
  verifying: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
  confirmed: 'bg-red-500/10 text-red-400 border-red-500/20',
  dismissed: 'bg-zinc-900 text-zinc-500 border-zinc-800',
  merged: 'bg-zinc-900 text-zinc-500 border-zinc-800',
  skipped: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
};

const reportsFetcher = async (key: string, signal: AbortSignal): Promise<ReportsResponse> => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load chat reports: ${res.status}`);
  const data = await res.json();
  return {
    enabled: Boolean(data.enabled),
    reports: data.reports || [],
    channels: data.channels || [],
    can_edit: Boolean(data.can_edit),
  };
};

function ChannelRow({ channel, canEdit, onChanged }: {
  readonly channel: Channel;
  readonly canEdit: boolean;
  readonly onChanged: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const id = `read-${channel.channel_id}`;

  const toggle = async (enabled: boolean) => {
    setSaving(true);
    try {
      await fetch(`/api/detection/chat-channels/${channel.channel_id}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
    } finally {
      setSaving(false);
      onChanged();
    }
  };

  return (
    <div className="flex items-center justify-between py-1.5">
      <Label htmlFor={id} className="text-xs text-zinc-300">#{channel.name}</Label>
      <div className="flex items-center gap-3">
        <span className="text-[11px] text-zinc-600">
          {channel.last_read_at ? `read ${formatTimeAgo(channel.last_read_at)}` : 'not read yet'}
        </span>
        <Switch id={id} checked={channel.enabled} disabled={!canEdit || saving} onCheckedChange={toggle} />
      </div>
    </div>
  );
}

/** Problem reports Aurora read in chat and what became of each. Renders nothing when intake is off. */
export default function ChatReports() {
  const { data, mutate } = useQuery<ReportsResponse>(
    '/api/detection/reports',
    reportsFetcher,
    { staleTime: 10_000, refreshInterval: 30_000, retryCount: 0 },
  );
  if (!data?.enabled) return null;

  return (
    <div className="mt-10">
      <h3 className="text-lg font-semibold">Reports from chat</h3>
      <p className="text-sm text-zinc-500 mt-1 mb-4">
        When someone says in a channel that something is wrong, Aurora checks whether it is, and opens an incident only if it is. It keeps one line about what was said, for 30 days.
      </p>

      {data.reports.length === 0 ? (
        <div className="text-sm text-zinc-500 py-8 text-center border border-zinc-800/80 rounded-xl bg-zinc-900/60">
          No reports read yet
        </div>
      ) : (
        <div className="space-y-2">
          {data.reports.map((report) => (
            <div key={report.id} className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4">
              <div className="flex items-start justify-between gap-4">
                <p className="text-sm text-zinc-200">{report.claim}</p>
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border whitespace-nowrap ${STATUS_STYLES[report.status]}`}>
                  {STATUS_LABEL[report.status] ?? report.status}
                </span>
              </div>
              <div className="flex items-center gap-3 mt-2 flex-wrap text-xs text-zinc-500">
                <span>#{report.channel}</span>
                <span>{formatTimeAgo(report.said_at)}</span>
                {report.asset && <span className="text-zinc-400">{report.asset}</span>}
                {report.incident_id && (
                  <Link href={`/incidents/${report.incident_id}`} className="text-sky-400 hover:underline">
                    Open the incident
                  </Link>
                )}
              </div>
              {report.status_detail && (
                <p className="text-xs text-zinc-500 mt-2 whitespace-pre-wrap">{report.status_detail}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {data.channels.length > 0 && (
        <div className="mt-6 bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4">
          <p className="text-[11px] uppercase tracking-wide text-zinc-600 mb-2">Channels Aurora reads</p>
          <p className="text-xs text-zinc-500 mb-2">Public channels Aurora is in. Private channels are never read.</p>
          {data.channels.map((channel) => (
            <ChannelRow key={channel.channel_id} channel={channel} canEdit={data.can_edit} onChanged={() => mutate()} />
          ))}
        </div>
      )}
    </div>
  );
}
