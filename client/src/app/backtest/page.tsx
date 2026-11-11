'use client';

import { useEffect, useRef, useState } from 'react';
import { History, Loader2, ShieldAlert, ShieldCheck, ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

type Kind = 'reverted' | 'control';

interface Candidate {
  pr_number: number;
  kind: Kind;
  title?: string;
  revert_pr_number?: number;
  revert_title?: string;
}

interface Finding {
  severity: string;
  file_path: string;
  line?: number | null;
  title: string;
  explanation: string;
}

interface Review {
  status: 'running' | 'completed' | 'failed';
  verdict?: 'SAFE' | 'RISKY';
  title?: string;
  url?: string;
  summary?: string;
  findings?: Finding[];
  error?: string;
  duration_ms?: number;
}

interface Row extends Candidate {
  jobId?: string;
  review?: Review;
}

const POLL_MS = 3000;

function outcome(row: Row): { label: string; tone: string } {
  const r = row.review;
  if (!r || r.status === 'running') return { label: 'Analyzing pre-merge diff…', tone: 'text-zinc-400' };
  if (r.status === 'failed') return { label: r.error || 'Failed', tone: 'text-zinc-500' };
  if (row.kind === 'reverted') {
    return r.verdict === 'RISKY'
      ? { label: 'Would have caught', tone: 'text-emerald-400' }
      : { label: 'Missed', tone: 'text-amber-400' };
  }
  return r.verdict === 'RISKY'
    ? { label: 'Flagged (possible false positive)', tone: 'text-amber-400' }
    : { label: 'Correctly quiet', tone: 'text-emerald-400' };
}

export default function BacktestPage() {
  const [repoUrl, setRepoUrl] = useState('');
  const [repo, setRepo] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [finding, setFinding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (timer.current) clearInterval(timer.current); }, []);

  const startReviews = async (repoName: string, candidates: Candidate[]) => {
    const started = await Promise.all(candidates.map(async (c): Promise<Row> => {
      const res = await fetch('/api/backtest/reviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repoName, pr_number: c.pr_number, kind: c.kind }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return { ...c, review: { status: 'failed', error: body.error || 'Could not start review' } };
      return { ...c, jobId: body.job_id, review: { status: 'running' } };
    }));
    setRows(started);

    let latest = started;
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(async () => {
      latest = await Promise.all(latest.map(async (row): Promise<Row> => {
        if (!row.jobId || row.review?.status !== 'running') return row;
        const res = await fetch(`/api/backtest/reviews/${row.jobId}`);
        if (!res.ok) return row;
        return { ...row, review: await res.json() };
      }));
      setRows(latest);
      if (latest.every((r) => r.review?.status !== 'running') && timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
    }, POLL_MS);
  };

  const findReverts = async () => {
    setFinding(true);
    setError(null);
    setRows([]);
    setRepo(null);
    try {
      const res = await fetch('/api/backtest/candidates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repoUrl }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || 'Lookup failed');
      const candidates: Candidate[] = [...body.reverted, ...body.controls];
      setRepo(body.repo);
      if (body.reverted.length === 0) {
        setError('No reverted PRs found in this repository. Try a larger or busier repo.');
        return;
      }
      setRows(candidates.map((c) => ({ ...c })));
      await startReviews(body.repo, candidates);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Lookup failed');
    } finally {
      setFinding(false);
    }
  };

  const done = (k: Kind) => rows.filter((r) => r.kind === k && r.review?.status === 'completed');
  const reverted = done('reverted');
  const controls = done('control');
  const caught = reverted.filter((r) => r.review?.verdict === 'RISKY').length;
  const falseFlags = controls.filter((r) => r.review?.verdict === 'RISKY').length;

  return (
    <div className="max-w-[1100px] mx-auto py-8 px-4 sm:px-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-100 flex items-center gap-2">
          <History size={22} /> Would Aurora have caught it?
        </h1>
        <p className="text-sm text-zinc-500 mt-1 max-w-3xl">
          Paste a public GitHub repo. Aurora finds PRs that were later reverted, plus merged PRs that were not,
          and reviews each one exactly as it looked before merge. No GitHub App, no credentials.
        </p>
      </div>

      <form
        className="flex gap-2 mb-2"
        onSubmit={(e) => { e.preventDefault(); if (repoUrl.trim()) findReverts(); }}
      >
        <Input
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder="https://github.com/owner/repo"
          className="max-w-xl"
        />
        <Button type="submit" disabled={finding || !repoUrl.trim()}>
          {finding ? <Loader2 className="animate-spin" size={16} /> : 'Find reverts'}
        </Button>
      </form>
      <p className="text-xs text-zinc-600 mb-6">
        Hindsight-proof: the review sees only the merge-time diff and title. No PR description, comments,
        repository HEAD, web search or tools.
      </p>

      {error && <p className="text-sm text-red-400 mb-4">{error}</p>}

      {repo && rows.length > 0 && (
        <div className="grid grid-cols-2 gap-3 mb-6">
          <div className="rounded-lg border border-zinc-800 p-4">
            <div className="text-xs text-zinc-500">Reverted PRs caught</div>
            <div className="text-2xl font-semibold text-zinc-100">{caught} / {reverted.length}</div>
          </div>
          <div className="rounded-lg border border-zinc-800 p-4">
            <div className="text-xs text-zinc-500">Control PRs flagged (lower is better)</div>
            <div className="text-2xl font-semibold text-zinc-100">{falseFlags} / {controls.length}</div>
          </div>
        </div>
      )}

      <ul className="space-y-3">
        {rows.map((row) => {
          const o = outcome(row);
          const r = row.review;
          const title = r?.title || row.title || row.revert_title;
          return (
            <li key={`${row.kind}-${row.pr_number}`} className="rounded-lg border border-zinc-800 p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="text-xs uppercase tracking-wide text-zinc-500">
                    {row.kind === 'reverted' ? `Reverted by #${row.revert_pr_number}` : 'Control (no revert found)'}
                  </div>
                  <div className="text-sm text-zinc-100 truncate">
                    #{row.pr_number} {title}
                    {r?.url && (
                      <a href={r.url} target="_blank" rel="noopener noreferrer" className="inline-flex ml-1 text-zinc-500 hover:text-zinc-300">
                        <ExternalLink size={12} />
                      </a>
                    )}
                  </div>
                </div>
                <div className={`flex items-center gap-1.5 text-sm whitespace-nowrap ${o.tone}`}>
                  {r?.status === 'running' && <Loader2 className="animate-spin" size={14} />}
                  {r?.verdict === 'RISKY' && <ShieldAlert size={14} />}
                  {r?.verdict === 'SAFE' && <ShieldCheck size={14} />}
                  {o.label}
                </div>
              </div>
              {r?.summary && <p className="text-sm text-zinc-400 mt-2">{r.summary}</p>}
              {r?.findings && r.findings.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {r.findings.map((f, i) => (
                    <li key={i} className="text-xs text-zinc-400">
                      <span className="font-medium text-zinc-200">[{f.severity}] {f.title}</span>{' '}
                      <span className="text-zinc-500">{f.file_path}{f.line ? `:${f.line}` : ''}</span>
                      <div>{f.explanation}</div>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
