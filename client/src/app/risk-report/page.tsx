'use client';

import { Suspense, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  ArrowRight, CheckCircle2, ExternalLink, EyeOff, FileCode, Github, History, Lightbulb, Loader2, ShieldAlert, XCircle,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { trackRiskReport } from '@/lib/risk-report';

type Mode = 'pr' | 'repo';

interface Finding { severity?: string; title?: string; file_path?: string; explanation?: string }
interface Evidence { ref: string; url: string; title: string; after_hours: number; reason?: string }
interface Hindsight { what_broke: string; visible_in_diff: boolean; verdict_gap: string; would_catch_it: string }
interface PRResult {
  number: number; title: string; url: string; merged_at: string;
  label: 'incident' | 'clean' | 'unknown' | 'open';
  outcome: 'caught' | 'missed' | 'false_alarm' | 'quiet' | 'flagged' | 'not_flagged' | 'error';
  evidence: Evidence[]; infra_files: boolean; verdict: 'RISKY' | 'SAFE' | null;
  summary: string; findings: Finding[]; error: string | null; seconds: number; session_id: string | null;
  files_reviewed?: string[]; hindsight?: Hindsight | null;
}
interface Score {
  caught: number; missed: number; false_alarm: number; quiet: number; error: number;
  incidents_reviewed: number; clean_reviewed: number;
}
interface Report {
  mode: Mode; repo: string; seconds: number; window_days?: number;
  score: Score; score_in_lane: Score; prs: PRResult[];
}
interface Run {
  id: string; mode: Mode; target: string; status: 'queued' | 'running' | 'done' | 'failed';
  progress: { stage: string; done?: number; total?: number; pr?: number; elapsed_s?: number };
  report: Report | null; error: string | null;
}

const EXAMPLES: Record<Mode, { label: string; value: string }[]> = {
  pr: [{ label: 'grafana#136249 (CI change, reverted 2 days later)', value: 'https://github.com/grafana/grafana/pull/136249' }],
  repo: [{ label: 'grafana/grafana', value: 'https://github.com/grafana/grafana' }],
};

const STAGES: Record<string, string> = {
  queued: 'Starting…',
  fetching_pr: 'Reading the PR as it was before merge…',
  checking_what_happened: 'Checking what happened after merge…',
  finding_reverts: 'Finding merged PRs that were later reverted…',
  finding_controls: 'Picking clean PRs to measure false alarms…',
  reviewing: 'Aurora is reviewing',
  done: 'Done',
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/proxy/risk-report/${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error || 'Request failed');
  return body as T;
}

export default function RiskReportPage() {
  return (
    <Suspense fallback={null}>
      <RiskReport />
    </Suspense>
  );
}

function RiskReport() {
  const params = useSearchParams();
  const [mode, setMode] = useState<Mode>(params.get('mode') === 'repo' ? 'repo' : 'pr');
  const [target, setTarget] = useState('');
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  const start = async () => {
    setError(null);
    setSubmitting(true);
    try {
      const { id } = await api<{ id: string }>('runs', {
        method: 'POST', body: JSON.stringify({ mode, target }),
      });
      if (poll.current) clearInterval(poll.current);
      const tick = async () => {
        const r = await api<Run>(`runs/${id}`);
        setRun(r);
        if (r.status === 'done' || r.status === 'failed') {
          if (poll.current) clearInterval(poll.current);
        }
      };
      await tick();
      poll.current = setInterval(() => { tick().catch(() => {}); }, 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the report');
    } finally {
      setSubmitting(false);
    }
  };

  const busy = submitting || run?.status === 'queued' || run?.status === 'running';

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <div className="mb-6">
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <History className="h-6 w-6 text-foreground" />
          Risk Report
        </h1>
        <p className="text-muted-foreground mt-2">
          See what Aurora would have flagged <em>before</em> a change merged. Just a link: no install,
          no credentials, nothing connected.
        </p>
      </div>

      <Card className="mb-6">
        <CardContent className="pt-6 space-y-4">
          <Tabs value={mode} onValueChange={v => { setMode(v as Mode); setTarget(''); setRun(null); setError(null); }}>
            <TabsList>
              <TabsTrigger value="pr">A PR you know</TabsTrigger>
              <TabsTrigger value="repo">A public repo</TabsTrigger>
            </TabsList>
          </Tabs>
          <p className="text-sm text-muted-foreground">
            {mode === 'pr'
              ? 'Paste a PR you know caused trouble, or didn’t. You hold the answer; Aurora only sees the PR as it was before merge.'
              : 'Not sure which PR? Aurora finds merged PRs that were later reverted, mixes in clean ones, and scores itself. Takes a few minutes.'}
          </p>
          <form className="flex gap-2" onSubmit={e => { e.preventDefault(); if (target.trim()) void start(); }}>
            <Input
              value={target}
              onChange={e => setTarget(e.target.value)}
              placeholder={mode === 'pr' ? 'https://github.com/owner/repo/pull/123' : 'https://github.com/owner/repo'}
              disabled={busy}
            />
            <Button type="submit" disabled={busy || !target.trim()}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Run'}
            </Button>
          </form>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="text-muted-foreground">Try:</span>
            {EXAMPLES[mode].map(ex => (
              <button key={ex.value} type="button" disabled={busy}
                className="underline text-muted-foreground hover:text-foreground"
                onClick={() => setTarget(ex.value)}>
                {ex.label}
              </button>
            ))}
          </div>
          <div className="flex items-start gap-2 rounded-md bg-muted/50 p-3 text-xs text-muted-foreground">
            <EyeOff className="h-4 w-4 mt-0.5 shrink-0" />
            <span>
              <strong className="text-foreground">What Aurora reads:</strong> the PR&apos;s title, description and diff
              as of before merge, from GitHub&apos;s public API. <strong className="text-foreground">What it never sees:</strong>{' '}
              comments, reviews, reverts, or anything after merge. Nothing is written to GitHub.
            </span>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>

      {run && run.status !== 'done' && <Progress run={run} />}
      {run?.status === 'done' && run.report && <ReportView report={run.report} />}
    </div>
  );
}

function Progress({ run }: Readonly<{ run: Run }>) {
  if (run.status === 'failed') {
    return (
      <Card><CardContent className="pt-6 flex items-center gap-2 text-sm text-destructive">
        <XCircle className="h-4 w-4" /> {run.error || 'The report failed.'}
      </CardContent></Card>
    );
  }
  const p = run.progress || { stage: 'queued' };
  const label = STAGES[p.stage] || p.stage;
  return (
    <Card><CardContent className="pt-6 flex items-center gap-3 text-sm">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span>
        {label}
        {p.stage === 'reviewing' && p.total ? ` PR ${(p.done ?? 0) + 1} of ${p.total} (#${p.pr})…` : ''}
      </span>
      {p.elapsed_s !== undefined && <span className="ml-auto text-muted-foreground">{Math.round(p.elapsed_s)}s</span>}
    </CardContent></Card>
  );
}

const VERDICT_HEADLINE: Record<'RISKY' | 'SAFE' | 'ERROR', string> = {
  RISKY: 'Aurora would have flagged this before merge.',
  SAFE: 'Aurora would have let this through.',
  ERROR: 'Aurora could not finish this review.',
};

function Headline({ report }: Readonly<{ report: Report }>) {
  if (report.mode === 'pr') {
    const pr = report.prs[0];
    return (
      <div>
        <p className="text-sm text-muted-foreground mb-1">{report.repo} · reviewed in {Math.round(report.seconds)}s</p>
        <p className="text-xl font-semibold">{VERDICT_HEADLINE[pr.verdict ?? 'ERROR']}</p>
      </div>
    );
  }
  const s = report.score;
  const lane = report.score_in_lane;
  return (
    <div>
      <p className="text-sm text-muted-foreground mb-1">
        {report.repo} · last {report.window_days} days · {report.prs.length} PRs in {Math.round(report.seconds)}s
      </p>
      <p className="text-xl font-semibold">
        Caught {s.caught} of {s.incidents_reviewed} PRs that were later reverted · {s.false_alarm} false alarm
        {s.false_alarm === 1 ? '' : 's'} in {s.clean_reviewed} clean PRs
      </p>
      {lane.incidents_reviewed + lane.clean_reviewed > 0 && (
        <p className="text-sm text-muted-foreground mt-1">
          In Aurora&apos;s lane (infra, CI, config, migrations): caught {lane.caught} of {lane.incidents_reviewed},
          {' '}{lane.false_alarm} false alarm{lane.false_alarm === 1 ? '' : 's'} in {lane.clean_reviewed}.
        </p>
      )}
    </div>
  );
}

const OUTCOME: Record<PRResult['outcome'], { text: string; cls: string }> = {
  caught: { text: 'Caught', cls: 'bg-green-500/15 text-green-600 dark:text-green-400' },
  missed: { text: 'Missed', cls: 'bg-red-500/15 text-red-600 dark:text-red-400' },
  false_alarm: { text: 'False alarm', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
  quiet: { text: 'Correctly quiet', cls: 'bg-muted text-muted-foreground' },
  flagged: { text: 'Flagged', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
  not_flagged: { text: 'Not flagged', cls: 'bg-muted text-muted-foreground' },
  error: { text: 'Review failed', cls: 'bg-muted text-muted-foreground' },
};

function AfterMerge({ pr }: Readonly<{ pr: PRResult }>) {
  if (pr.label === 'incident' && pr.evidence[0]) {
    const e = pr.evidence[0];
    return (
      <>
        <span>
          Reverted in{' '}
          <a href={e.url} target="_blank" rel="noreferrer" className="underline">{e.ref}</a>
          {' '}{e.after_hours < 48 ? `${Math.round(e.after_hours)}h` : `${Math.round(e.after_hours / 24)} days`} later
        </span>
        <span className="block mt-1 text-muted-foreground">
          {e.reason ? <>The team wrote: <q className="text-foreground">{e.reason}</q></> : 'No reason given in the revert.'}
        </span>
      </>
    );
  }
  if (pr.label === 'clean') return <span>Not reverted (control)</span>;
  if (pr.label === 'open') return <span>Still open: this is a live pre-merge review</span>;
  return <span>Not reverted. You know whether it caused trouble.</span>;
}

function PRRow({ pr }: Readonly<{ pr: PRResult }>) {
  const o = OUTCOME[pr.outcome];
  return (
    <div className="border-t border-border py-4 first:border-t-0">
      <div className="flex items-start justify-between gap-3">
        <a href={pr.url} target="_blank" rel="noreferrer" className="font-medium hover:underline">
          #{pr.number} {pr.title}
        </a>
        <div className="flex items-center gap-2 shrink-0">
          {pr.infra_files && <Badge variant="outline" className="text-xs">in lane</Badge>}
          <span className={`text-xs font-medium px-2 py-0.5 rounded ${o.cls}`}>{o.text}</span>
        </div>
      </div>
      <div className="grid sm:grid-cols-2 gap-4 mt-3 text-sm">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">Before merge, Aurora said</p>
          {pr.verdict ? (
            <>
              <p className="flex items-center gap-1 font-medium">
                {pr.verdict === 'RISKY'
                  ? <><ShieldAlert className="h-4 w-4 text-amber-500" /> Risky</>
                  : <><CheckCircle2 className="h-4 w-4 text-muted-foreground" /> Safe to merge</>}
              </p>
              {pr.findings.map(f => (
                <div key={`${f.file_path ?? ''}:${f.title ?? ''}`} className="mt-2">
                  <p>
                    <span className="text-xs font-semibold">{f.severity}</span> {f.title}
                    {f.file_path && <span className="text-muted-foreground"> · {f.file_path}</span>}
                  </p>
                  {f.explanation && <p className="text-muted-foreground">{f.explanation}</p>}
                </div>
              ))}
              {pr.summary && <p className="text-muted-foreground mt-2">{pr.summary}</p>}
            </>
          ) : (
            <p className="text-muted-foreground">{pr.error || 'No verdict'}</p>
          )}
          {!!pr.files_reviewed?.length && (
            <p className="flex items-start gap-1 text-xs text-muted-foreground mt-2">
              <FileCode className="h-3 w-3 mt-0.5 shrink-0" />
              <span>
                Read {pr.files_reviewed.length} changed file{pr.files_reviewed.length === 1 ? '' : 's'}:{' '}
                {pr.files_reviewed.slice(0, 4).join(', ')}
                {pr.files_reviewed.length > 4 ? ` +${pr.files_reviewed.length - 4} more` : ''}
              </span>
            </p>
          )}
          {pr.session_id && (
            <Link href={`/chat?sessionId=${pr.session_id}`} className="inline-flex items-center gap-1 text-xs underline mt-2">
              See full reasoning <ExternalLink className="h-3 w-3" />
            </Link>
          )}
        </div>
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">After merge</p>
          <div><AfterMerge pr={pr} /></div>
        </div>
      </div>
      {pr.hindsight && <LookingBack h={pr.hindsight} />}
    </div>
  );
}

function LookingBack({ h }: Readonly<{ h: Hindsight }>) {
  return (
    <div className="mt-4 rounded-md border border-dashed border-border p-3 text-sm">
      <p className="flex items-center gap-1 text-xs uppercase tracking-wide text-muted-foreground mb-2">
        <Lightbulb className="h-3 w-3" /> Looking back
        <span className="normal-case tracking-normal">(written after the verdict, with the revert in view)</span>
      </p>
      <dl className="grid sm:grid-cols-[10rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">What broke</dt><dd>{h.what_broke}</dd>
        <dt className="text-muted-foreground">Visible in the diff?</dt>
        <dd>{h.visible_in_diff ? 'Yes, the change is in the diff' : 'No, it only shows up at runtime or scale'}</dd>
        <dt className="text-muted-foreground">Why the verdict differed</dt><dd>{h.verdict_gap}</dd>
        <dt className="text-muted-foreground">What would catch it</dt><dd>{h.would_catch_it}</dd>
      </dl>
    </div>
  );
}

function InstallCTA({ mode }: Readonly<{ mode: Mode }>) {
  return (
    <Card className="mt-6 border-primary/40">
      <CardContent className="pt-6 flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="flex-1">
          <p className="font-semibold">Get this on every PR, before it merges</p>
          <p className="text-sm text-muted-foreground">
            Install the GitHub app and Aurora reviews each new pull request for the incidents it could cause.
            Read-only on code, no production credentials.
          </p>
        </div>
        <Button asChild onClick={() => trackRiskReport('install_cta_clicked', { mode, source: 'report' })}>
          <Link href="/connectors?connect=github">
            <Github className="h-4 w-4 mr-2" /> Install on GitHub <ArrowRight className="h-4 w-4 ml-1" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}

function ReportView({ report }: Readonly<{ report: Report }>) {
  return (
    <>
      <Card>
        <CardContent className="pt-6">
          <Headline report={report} />
          <div className="mt-4">
            {report.prs.map(pr => <PRRow key={pr.number} pr={pr} />)}
          </div>
          {report.mode === 'repo' && report.prs.length === 0 && (
            <p className="text-sm text-muted-foreground mt-4">
              No reverted PRs found in this window. Teams that fix forward instead of reverting won&apos;t show up
              here yet. Try pasting a PR you know caused trouble.
            </p>
          )}
        </CardContent>
      </Card>
      <InstallCTA mode={report.mode} />
    </>
  );
}
