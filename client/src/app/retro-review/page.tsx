'use client';

import { useState, useCallback } from 'react';
import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  Loader2,
  ShieldAlert,
  XCircle,
} from 'lucide-react';
import { useQuery, fetchR, jsonFetcher } from '@/lib/query';
import { useToast } from '@/hooks/use-toast';
import { formatTimeAgo } from '@/lib/utils/time-format';

type JobStatus = 'pending' | 'running' | 'completed' | 'failed';

interface Finding {
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
  file_path: string;
  line: number | null;
  title: string;
  explanation: string;
}

interface Item {
  pr_number: number;
  kind: 'reverted' | 'control';
  status: 'pending' | 'reviewed' | 'skipped' | 'error' | 'out_of_scope';
  infra_files?: string[];
  title?: string;
  url?: string;
  merged_at?: string;
  revert_pr_number?: number;
  revert_url?: string;
  verdict?: 'SAFE' | 'RISKY';
  summary?: string;
  findings?: Finding[];
  reason?: string;
}

interface Summary {
  reverted_reviewed: number;
  caught: number;
  catch_rate: number | null;
  controls_reviewed: number;
  false_alarms: number;
  false_alarm_rate: number | null;
  skipped: number;
  errored: number;
  out_of_scope?: number;
}

interface Job {
  id: string;
  repo: string;
  status: JobStatus;
  summary: Summary | null;
  error: string | null;
  created_at: string | null;
  completed_at: string | null;
  items?: Item[];
}

const isActive = (status?: JobStatus) => status === 'pending' || status === 'running';
const pct = (rate: number | null) => (rate === null ? '—' : `${Math.round(rate * 100)}%`);

// ─── Report card ───────────────────────────────────────────────────

function Stat({ label, value, detail }: { readonly label: string; readonly value: string; readonly detail: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-zinc-100">{value}</div>
      <div className="mt-0.5 text-xs text-zinc-500">{detail}</div>
    </div>
  );
}

function VerdictBadge({ item }: { readonly item: Item }) {
  if (item.status === 'pending') {
    return <span className="flex items-center gap-1 text-xs text-zinc-500"><Loader2 className="h-3 w-3 animate-spin" /> Reviewing</span>;
  }
  if (item.status !== 'reviewed') {
    return <span className="text-xs text-zinc-500" title={item.reason}>{item.status === 'error' ? 'Error' : 'Skipped'}</span>;
  }
  // A catch is good on a reverted PR; RISKY on a control is a false alarm.
  const risky = item.verdict === 'RISKY';
  const good = item.kind === 'reverted' ? risky : !risky;
  const label = item.kind === 'reverted' ? (risky ? 'Caught' : 'Missed') : (risky ? 'False alarm' : 'Quiet');
  return (
    <span className={`flex items-center gap-1 text-xs font-medium ${good ? 'text-emerald-400' : 'text-amber-400'}`}>
      {good ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {label}
    </span>
  );
}

function ItemRow({ item }: { readonly item: Item }) {
  const [open, setOpen] = useState(false);
  const findings = item.findings ?? [];
  return (
    <div className="border-b border-zinc-800/70 last:border-0">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between gap-3 py-2.5 text-left hover:bg-zinc-900/40 px-2 rounded"
      >
        <div className="min-w-0">
          <div className="truncate text-sm text-zinc-200">
            <span className="text-zinc-500">#{item.pr_number}</span> {item.title || '…'}
          </div>
          {item.kind === 'reverted' && item.revert_pr_number && (
            <div className="text-xs text-zinc-600">reverted by #{item.revert_pr_number}</div>
          )}
        </div>
        <VerdictBadge item={item} />
      </button>
      {open && (
        <div className="px-2 pb-3 space-y-2 text-xs text-zinc-400">
          {item.summary && <p>{item.summary}</p>}
          {item.reason && <p className="text-zinc-500">{item.reason}</p>}
          {item.infra_files && item.infra_files.length > 0 && (
            <p className="text-zinc-600">Infra files: {item.infra_files.join(', ')}</p>
          )}
          {findings.map((f, i) => (
            <div key={i} className="rounded border border-zinc-800 p-2">
              <div className="font-medium text-zinc-300">
                <span className="text-amber-400">{f.severity}</span> {f.title}
              </div>
              <div className="text-zinc-500">{f.file_path}{f.line ? `:${f.line}` : ''}</div>
              <p className="mt-1">{f.explanation}</p>
            </div>
          ))}
          <div className="flex gap-3">
            {item.url && (
              <a href={item.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-zinc-400 hover:text-zinc-200">
                PR <ExternalLink className="h-3 w-3" />
              </a>
            )}
            {item.revert_url && (
              <a href={item.revert_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-zinc-400 hover:text-zinc-200">
                Revert <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function JobDetail({ jobId, onBack }: { readonly jobId: string; readonly onBack: () => void }) {
  const { data: job } = useQuery<Job>(`/api/retro-reviews/${jobId}`, jsonFetcher, { staleTime: 2_000 });
  // Poll while the job runs so results appear PR by PR.
  useQuery<Job>(isActive(job?.status) ? `/api/retro-reviews/${jobId}` : null, jsonFetcher, {
    refreshInterval: 3_000,
    staleTime: 1_000,
  });

  if (!job) return <div className="text-sm text-zinc-500 py-12 text-center">Loading...</div>;

  const s = job.summary;
  const allItems = job.items ?? [];
  const items = allItems.filter(i => i.status !== 'out_of_scope');
  const outOfScope = allItems.length - items.length;
  const reverted = items.filter(i => i.kind === 'reverted');
  const controls = items.filter(i => i.kind === 'control');

  return (
    <div className="space-y-6">
      <div>
        <button onClick={onBack} className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors mb-3">
          <ArrowLeft className="h-3 w-3" /> All retro reviews
        </button>
        <div className="flex items-center gap-2">
          <h2 className="text-xl font-semibold tracking-tight text-zinc-100">{job.repo}</h2>
          {isActive(job.status) && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
        </div>
        {job.error && <p className="text-sm text-amber-400 mt-1">{job.error}</p>}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Stat
          label="Caught before merge"
          value={s ? `${s.caught} of ${s.reverted_reviewed}` : '—'}
          detail={`${pct(s?.catch_rate ?? null)} of PRs that were later reverted`}
        />
        <Stat
          label="False alarms"
          value={s ? `${s.false_alarms} of ${s.controls_reviewed}` : '—'}
          detail={`${pct(s?.false_alarm_rate ?? null)} of PRs that were never reverted`}
        />
      </div>
      <p className="text-xs text-zinc-600">
        Each PR is reviewed as it looked at merge time, with no tools and no connections, so these numbers are a
        floor: a connected Aurora also sees your alerts, deploys and past incidents. Aurora reviews for deployment and
        infrastructure risk, not general code bugs, so only PRs that touch infra or deploy files are counted
        {outOfScope > 0 && ` (${outOfScope} app-code-only PR${outOfScope === 1 ? '' : 's'} left out)`}.
      </p>

      {[{ title: 'Reverted later', list: reverted }, { title: 'Never reverted (control)', list: controls }].map(group => (
        <div key={group.title}>
          <h3 className="text-sm font-medium text-zinc-300 mb-1">{group.title}</h3>
          <div className="rounded-lg border border-zinc-800 px-2">
            {group.list.length === 0
              ? <div className="py-3 text-xs text-zinc-600 px-2">{isActive(job.status) ? 'Finding PRs…' : 'None found'}</div>
              : group.list.map(item => <ItemRow key={`${item.kind}-${item.pr_number}`} item={item} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Start form + history ──────────────────────────────────────────

function RetroReviewContent() {
  const { toast } = useToast();
  const [repo, setRepo] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data, mutate } = useQuery<{ retro_reviews: Job[] }>('/api/retro-reviews', jsonFetcher, { staleTime: 5_000 });
  const jobs = data?.retro_reviews ?? [];

  const start = useCallback(async () => {
    if (!repo.trim()) return;
    setSubmitting(true);
    try {
      const res = await fetchR('/api/retro-reviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo }),
        retries: 0,
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 409 && body.id) {
        toast({ title: 'A retro review is already running' });
        setSelectedId(body.id);
      } else if (!res.ok) {
        toast({ title: body.error || 'Could not start the retro review', variant: 'destructive' });
      } else {
        setSelectedId(body.id);
        setRepo('');
        mutate();
      }
    } catch {
      toast({ title: 'Could not start the retro review', variant: 'destructive' });
    } finally {
      setSubmitting(false);
    }
  }, [repo, toast, mutate]);

  if (selectedId) {
    return <JobDetail jobId={selectedId} onBack={() => { setSelectedId(null); mutate(); }} />;
  }

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-2xl font-bold">Retro Review</h2>
        <p className="text-sm text-zinc-500 mt-1">
          See what Aurora would have flagged on your past pull requests, before you install anything. Enter a public
          GitHub repo: Aurora finds PRs that were later reverted, reviews each one as it looked at merge time, and checks
          the same number of PRs that were never reverted to count false alarms.
        </p>
      </div>

      <form
        onSubmit={e => { e.preventDefault(); start(); }}
        className="flex flex-col sm:flex-row gap-2"
      >
        <input
          value={repo}
          onChange={e => setRepo(e.target.value)}
          placeholder="owner/repo or https://github.com/owner/repo"
          className="flex-1 rounded-lg bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-600"
        />
        <button
          type="submit"
          disabled={submitting || !repo.trim()}
          className="flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-zinc-800 border border-zinc-700/50 text-sm font-medium text-zinc-200 hover:bg-zinc-700/80 disabled:opacity-50 transition-all"
        >
          {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldAlert className="h-4 w-4" />}
          Run retro review
        </button>
      </form>
      <p className="text-xs text-zinc-600 -mt-6">
        Reads public data only. No GitHub app, no token, nothing is posted to the repo.
      </p>

      <div>
        <h3 className="text-sm font-medium text-zinc-300 mb-2">Recent</h3>
        {jobs.length === 0 ? (
          <div className="text-xs text-zinc-600">No retro reviews yet.</div>
        ) : (
          <div className="rounded-lg border border-zinc-800 divide-y divide-zinc-800/70">
            {jobs.map(job => (
              <button
                key={job.id}
                onClick={() => setSelectedId(job.id)}
                className="w-full flex items-center justify-between px-3 py-2.5 text-left hover:bg-zinc-900/40"
              >
                <div>
                  <div className="text-sm text-zinc-200">{job.repo}</div>
                  <div className="text-xs text-zinc-600">{job.created_at ? formatTimeAgo(job.created_at) : ''}</div>
                </div>
                <div className="text-xs text-zinc-400">
                  {isActive(job.status) && <span className="flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" /> Running</span>}
                  {job.status === 'failed' && <span className="text-amber-400">Failed</span>}
                  {job.status === 'completed' && job.summary && (
                    <span>caught {job.summary.caught}/{job.summary.reverted_reviewed} · false alarms {job.summary.false_alarms}/{job.summary.controls_reviewed}</span>
                  )}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function RetroReviewPage() {
  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <RetroReviewContent />
    </div>
  );
}
