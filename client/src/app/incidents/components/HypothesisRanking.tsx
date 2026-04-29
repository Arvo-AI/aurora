'use client';

import { useMemo } from 'react';
import { CheckCircle2, XCircle, CircleSlash, Loader2 } from 'lucide-react';
import type { SubAgentRun } from '@/lib/services/incidents';

interface HypothesisRankingProps {
  runs: SubAgentRun[];
}

const STRENGTH_ORDER: Record<string, number> = {
  high: 0,
  medium: 1,
  low: 2,
  inconclusive: 3,
};

function statusIcon(status: SubAgentRun['status']) {
  switch (status) {
    case 'succeeded':
      return <CheckCircle2 className="w-3.5 h-3.5 text-green-400" />;
    case 'failed':
      return <XCircle className="w-3.5 h-3.5 text-red-400" />;
    case 'cancelled':
      return <CircleSlash className="w-3.5 h-3.5 text-zinc-400" />;
    case 'running':
      return <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin" />;
  }
}

function strengthBadgeClass(strength: SubAgentRun['self_assessed_strength']): string {
  switch (strength) {
    case 'high':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    case 'medium':
      return 'bg-blue-500/10 text-blue-400 border-blue-500/30';
    case 'low':
      return 'bg-yellow-500/10 text-yellow-400 border-yellow-500/30';
    case 'inconclusive':
      return 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30';
    default:
      return 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30';
  }
}

export default function HypothesisRanking({ runs }: HypothesisRankingProps) {
  const sorted = useMemo(() => {
    const subagents = runs.filter((r) => r.role !== 'main');
    return [...subagents].sort((a, b) => {
      const aSucceeded = a.status === 'succeeded' ? 0 : 1;
      const bSucceeded = b.status === 'succeeded' ? 0 : 1;
      if (aSucceeded !== bSucceeded) return aSucceeded - bSucceeded;
      const aRank = a.self_assessed_strength ? STRENGTH_ORDER[a.self_assessed_strength] ?? 4 : 4;
      const bRank = b.self_assessed_strength ? STRENGTH_ORDER[b.self_assessed_strength] ?? 4 : 4;
      return aRank - bRank;
    });
  }, [runs]);

  if (!sorted.length) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-4 text-xs text-zinc-500">
        No hypotheses to rank yet.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 space-y-2">
      <p className="text-[11px] text-zinc-500 uppercase tracking-wider mb-1">Hypothesis ranking</p>
      {sorted.map((run) => {
        const label = run.ui_label || run.agent_id;
        const isFailed = run.status === 'failed' || run.status === 'cancelled';
        // TODO: wire `cited` to the orchestrator's final-synthesis citation list
        // once that signal lands; for now we approximate from self-assessed strength.
        const cited = run.status === 'succeeded' && (
          run.self_assessed_strength === 'high' || run.self_assessed_strength === 'medium'
        );

        return (
          <div
            key={run.agent_id}
            className="flex items-start gap-2 px-2.5 py-2 rounded border border-zinc-800/80 bg-zinc-950/40"
          >
            <div className="shrink-0 mt-0.5">{statusIcon(run.status)}</div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm text-zinc-200 truncate">{label}</span>
                {run.self_assessed_strength && (
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${strengthBadgeClass(run.self_assessed_strength)}`}
                  >
                    strength: {run.self_assessed_strength}
                  </span>
                )}
                {cited && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-medium border bg-orange-500/10 text-orange-400 border-orange-500/30">
                    cited
                  </span>
                )}
              </div>
              {isFailed && run.error && (
                <p className="text-xs text-red-400/80 mt-1 break-words">{run.error}</p>
              )}
              {!isFailed && run.purpose && (
                <p className="text-xs text-zinc-500 mt-1 line-clamp-2">{run.purpose}</p>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
