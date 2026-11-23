'use client';

import Link from 'next/link';
import { useQuery } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';
import { ALERTING_KEY, alertingFetcher, AlertingResponse, SlackReport } from '@/components/DynamicAlerting';

type Report = SlackReport;
type ReportStatus = Report['status'];

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

function ReportRow({ report }: { readonly report: Report }) {
  const ended = !['new', 'verifying'].includes(report.status);
  const outcome = report.kind === 'question' && report.status === 'dismissed' ? 'Nothing to add' : OUTCOME[report.status];
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

/** What Aurora noticed in Slack and what it did about each: the record behind the
 *  Dynamic alerting slider in Settings. Renders nothing when the install has it
 *  off, the org has no Slack, or the slider is off and nothing was ever noticed. */
export default function AuroraInSlack() {
  const { data } = useQuery<AlertingResponse>(
    ALERTING_KEY,
    alertingFetcher,
    { staleTime: 10_000, refreshInterval: 30_000, retryCount: 0 },
  );
  if (!data?.enabled) return null;
  if (data.level === 'off' && data.reports.length === 0) return null;

  return (
    <section className="mt-12" aria-labelledby="aurora-in-slack">
      <h3 id="aurora-in-slack" className="text-lg font-semibold">Noticed in Slack</h3>
      <p className="text-sm text-zinc-500 mt-1 mb-4">
        What people said that Aurora looked into, what it found, and what it replied. Kept for 30 days. Messages and their authors are not stored.
        How much Aurora does is the Dynamic alerting slider in Settings, under Organization.
      </p>
      {data.reports.length === 0 ? (
        <div className={`${CARD} text-sm text-zinc-500 py-8 text-center`}>Nothing noticed yet</div>
      ) : (
        <div className="space-y-2">
          {data.reports.map((report) => <ReportRow key={report.id} report={report} />)}
        </div>
      )}
    </section>
  );
}
