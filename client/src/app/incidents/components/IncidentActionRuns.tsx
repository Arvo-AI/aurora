'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ExternalLink, CheckCircle2, AlertCircle, Clock } from 'lucide-react';

interface ActionRun {
  id: string;
  action_id: string;
  action_name: string;
  status: string;
  chat_session_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms?: number;
  error: string | null;
}

interface IncidentActionRunsProps {
  readonly incidentId: string;
}

function StatusIcon({ status }: { readonly status: string }) {
  switch (status) {
    case 'success':
      return <CheckCircle2 className="w-3.5 h-3.5 text-green-400" />;
    case 'error':
      return <AlertCircle className="w-3.5 h-3.5 text-red-400" />;
    case 'running':
      return (
        <span className="relative flex h-3.5 w-3.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-orange-400 opacity-75"></span>
          <span className="relative inline-flex rounded-full h-3.5 w-3.5 bg-orange-500"></span>
        </span>
      );
    default:
      return <Clock className="w-3.5 h-3.5 text-zinc-500" />;
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

function formatTime(isoString: string): string {
  const date = new Date(isoString);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export default function IncidentActionRuns({ incidentId }: IncidentActionRunsProps) {
  const [runs, setRuns] = useState<ActionRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;

    async function fetchRuns() {
      try {
        const res = await fetch(`/api/incidents/${incidentId}/action-runs`);
        if (!res.ok) {
          if (!cancelled) setError(true);
          return;
        }
        const data = await res.json();
        if (!cancelled) {
          setRuns(data.runs || []);
          setError(false);

          // Poll every 5s while any run is still in progress
          const hasInProgress = (data.runs || []).some(
            (r: ActionRun) => r.status === 'pending' || r.status === 'running'
          );
          if (hasInProgress) {
            pollTimer = setTimeout(fetchRuns, 5000);
          }
        }
      } catch {
        if (!cancelled) setError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    fetchRuns();
    return () => {
      cancelled = true;
      if (pollTimer) clearTimeout(pollTimer);
    };
  }, [incidentId]);

  if (loading) {
    return (
      <div className="py-4 text-center text-xs text-zinc-500">Loading actions...</div>
    );
  }

  if (error) {
    return (
      <div className="py-4 text-center text-xs text-red-400">Failed to load action runs.</div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="py-4 text-center text-xs text-zinc-500">No actions have run for this incident.</div>
    );
  }

  return (
    <div className="space-y-2">
      {runs.map((run) => (
        <div
          key={run.id}
          className="flex items-center justify-between gap-3 px-3 py-2 rounded-md bg-zinc-900/50 border border-zinc-800/50"
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <StatusIcon status={run.status} />
            <span className="text-sm text-zinc-200 truncate">{run.action_name}</span>
            {run.duration_ms != null && (
              <span className="text-[11px] text-zinc-500 shrink-0">
                {formatDuration(run.duration_ms)}
              </span>
            )}
            {run.started_at && (
              <span className="text-[11px] text-zinc-600 shrink-0">
                {formatTime(run.started_at)}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {run.error && (
              <span className="text-[11px] text-red-400 max-w-[150px] truncate" title={run.error}>
                {run.error}
              </span>
            )}
            {run.chat_session_id && (
              <Link
                href={`/chat?sessionId=${encodeURIComponent(run.chat_session_id)}`}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                View
                <ExternalLink className="w-2.5 h-2.5" />
              </Link>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
