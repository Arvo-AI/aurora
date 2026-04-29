'use client';

import { ChevronRight, ChevronDown, Loader2, CheckCircle2, XCircle, CircleSlash } from 'lucide-react';
import type { SubAgentRun, SubAgentRunStatus } from '@/lib/services/incidents';

interface SubAgentCardProps {
  run: SubAgentRun;
  onClick?: () => void;
  expanded?: boolean;
}

function statusVisual(status: SubAgentRunStatus) {
  switch (status) {
    case 'running':
      return {
        icon: <Loader2 className="w-3 h-3 animate-spin" />,
        label: 'running',
        cls: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
      };
    case 'succeeded':
      return {
        icon: <CheckCircle2 className="w-3 h-3" />,
        label: 'succeeded',
        cls: 'bg-green-500/10 text-green-400 border-green-500/30',
      };
    case 'failed':
      return {
        icon: <XCircle className="w-3 h-3" />,
        label: 'failed',
        cls: 'bg-red-500/10 text-red-400 border-red-500/30',
      };
    case 'cancelled':
      return {
        icon: <CircleSlash className="w-3 h-3" />,
        label: 'cancelled',
        cls: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30',
      };
  }
}

function strengthVisual(strength: SubAgentRun['self_assessed_strength']) {
  if (!strength) return null;
  switch (strength) {
    case 'high':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    case 'medium':
      return 'bg-blue-500/10 text-blue-400 border-blue-500/30';
    case 'low':
      return 'bg-yellow-500/10 text-yellow-400 border-yellow-500/30';
    case 'inconclusive':
      return 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30';
  }
}

function formatRelativeAgo(timestamp: string): string {
  const diffMs = Date.now() - new Date(timestamp).getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

function formatDuration(start: string, end: string): string {
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 0) return '0s';
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min < 60) return sec > 0 ? `${min}m ${sec}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

export default function SubAgentCard({ run, onClick, expanded }: SubAgentCardProps) {
  const status = statusVisual(run.status);
  const strengthCls = strengthVisual(run.self_assessed_strength);
  const label = run.ui_label || run.agent_id;
  const model = run.model_used;
  const interactive = Boolean(onClick);

  let timing: string | null = null;
  if (run.started_at && run.ended_at) {
    timing = `ran for ${formatDuration(run.started_at, run.ended_at)}`;
  } else if (run.started_at) {
    timing = `started ${formatRelativeAgo(run.started_at)}`;
  }

  return (
    <div
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={onClick}
      onKeyDown={interactive ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick?.();
        }
      } : undefined}
      className={`flex items-start gap-3 px-3 py-2.5 rounded-lg border border-zinc-800 bg-zinc-900/40 transition-colors ${
        interactive ? 'cursor-pointer hover:bg-zinc-900/80 hover:border-zinc-700' : ''
      }`}
    >
      <span
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium border shrink-0 mt-0.5 ${status.cls}`}
      >
        {status.icon}
        {status.label}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-zinc-200 truncate">{label}</span>
          {strengthCls && run.self_assessed_strength && (
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${strengthCls}`}>
              strength: {run.self_assessed_strength}
            </span>
          )}
          {run.role === 'main' && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-medium border bg-orange-500/10 text-orange-400 border-orange-500/30">
              main
            </span>
          )}
        </div>

        {run.purpose && (
          <p className="text-xs text-zinc-400 mt-1 line-clamp-2">{run.purpose}</p>
        )}

        <div className="flex items-center gap-3 mt-1.5 text-[11px] text-zinc-500">
          {model && <span className="font-mono truncate">{model}</span>}
          {timing && <span>{timing}</span>}
        </div>
      </div>

      {interactive && (
        <div className="shrink-0 mt-1 text-zinc-500">
          {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </div>
      )}
    </div>
  );
}
