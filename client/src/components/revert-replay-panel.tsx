'use client';

import { useCallback, useEffect, useState } from 'react';
import { Loader2, RotateCw } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

// Revert replay: when Incident Prevention is turned on, Aurora replays its
// pre-merge review over the repo's recently reverted PRs (and a control set of
// PRs that shipped fine). This panel shows that first result where the user
// just flipped the switch, instead of an empty state until the next PR.

interface ReplayFinding {
  title?: string;
  severity?: string;
}

interface ReplayResult {
  status: string;
  verdict?: 'SAFE' | 'RISKY' | null;
  findings?: ReplayFinding[];
  error?: string | null;
  case: {
    pr_number: number;
    title: string;
    label: 'reverted' | 'control';
    revert_pr_number?: number | null;
    revert_reason?: string | null;
  };
}

interface ReplayReport {
  status: 'none' | 'queued' | 'running' | 'completed' | 'failed';
  headline?: string;
  total_cases?: number;
  results?: ReplayResult[];
  scores?: {
    reverted_reviewed: number;
    reverted_flagged: number;
    controls_reviewed: number;
    controls_flagged: number;
  };
}

const POLL_MS = 5000;

function replayUrl(repoFullName: string) {
  return `/api/proxy/github/repo-selections/${encodeURIComponent(repoFullName)}/replay`;
}

function prUrl(repoFullName: string, n: number) {
  return `https://github.com/${repoFullName}/pull/${n}`;
}

export function RevertReplayPanel({ repoFullName }: { repoFullName: string }) {
  const [report, setReport] = useState<ReplayReport | null>(null);
  const [rerunning, setRerunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch(replayUrl(repoFullName));
      if (response.ok) setReport(await response.json());
    } catch {
      // Best-effort panel: a failed poll just keeps the last report.
    }
  }, [repoFullName]);

  useEffect(() => {
    load();
  }, [load]);

  const active = report?.status === 'queued' || report?.status === 'running';
  useEffect(() => {
    if (!active) return;
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [active, load]);

  const rerun = async () => {
    setRerunning(true);
    try {
      await fetch(replayUrl(repoFullName), { method: 'POST' });
      await load();
    } finally {
      setRerunning(false);
    }
  };

  if (!report || report.status === 'none') return null;

  const results = report.results ?? [];
  const reverted = results.filter(r => r.case.label === 'reverted');
  const controls = results.filter(r => r.case.label === 'control');

  return (
    <div
      className="mt-2 rounded-md border border-border/60 bg-muted/30 p-2 space-y-2"
      data-testid={`revert-replay-${repoFullName}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="text-xs">
          <p className="font-medium">Replay on your reverted PRs</p>
          {active && (
            <p className="text-muted-foreground flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin" />
              Reviewing {results.length} of {report.total_cases ?? '…'} past PRs as if they were still open
            </p>
          )}
          {report.status === 'failed' && (
            <p className="text-muted-foreground">The replay could not finish. Run it again.</p>
          )}
          {report.status === 'completed' && report.headline && (
            <p className="text-foreground">{report.headline}</p>
          )}
        </div>
        {!active && (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-xs"
            disabled={rerunning}
            onClick={rerun}
          >
            <RotateCw className="h-3 w-3 mr-1" />
            Run again
          </Button>
        )}
      </div>

      {reverted.length > 0 && (
        <ul className="space-y-1">
          {reverted.map(r => {
            const top = r.findings?.[0]?.title;
            return (
              <li key={r.case.pr_number} className="text-xs flex items-start gap-2">
                <VerdictBadge result={r} />
                <div className="min-w-0">
                  <a
                    href={prUrl(repoFullName, r.case.pr_number)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:underline"
                  >
                    #{r.case.pr_number} {r.case.title}
                  </a>
                  {r.case.revert_pr_number != null && (
                    <span className="text-muted-foreground"> (reverted in #{r.case.revert_pr_number})</span>
                  )}
                  {top && <p className="text-muted-foreground truncate">Aurora: {top}</p>}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {controls.length > 0 && report.scores && (
        <p className="text-[11px] text-muted-foreground">
          Control: flagged {report.scores.controls_flagged} of {report.scores.controls_reviewed} PRs that
          were never reverted. Replay only reads your PRs; it posts nothing to GitHub.
        </p>
      )}
    </div>
  );
}

function VerdictBadge({ result }: { result: ReplayResult }) {
  if (result.status !== 'completed' || !result.verdict) {
    return <Badge variant="outline" className="h-5 px-1.5 text-[10px]">error</Badge>;
  }
  if (result.verdict === 'RISKY') {
    return <Badge variant="destructive" className="h-5 px-1.5 text-[10px]">caught</Badge>;
  }
  return <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">missed</Badge>;
}
