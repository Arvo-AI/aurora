'use client';

import React, { useEffect, useState } from 'react';
import {
  Brain,
  Terminal,
  ChevronDown,
  ChevronRight,
  Loader2,
} from 'lucide-react';

interface ExecutionStep {
  type: 'thought' | 'tool_call';
  // Backend returns null for any of these when the underlying row is sparse;
  // the component must guard before reading length / constructing Date.
  timestamp: string | null;
  toolName: string | null;
  command: string | null;
  content: string | null;
}

interface LifecycleEvent {
  eventType: string;
  previousValue: string | null;
  newValue: string | null;
  timestamp: string | null;
}

interface AgentExecutionData {
  steps: ExecutionStep[];
  lifecycle: LifecycleEvent[];
}

function formatRelativeTime(baseTime: Date, stepTime: Date): string {
  const diffMs = stepTime.getTime() - baseTime.getTime();
  if (diffMs < 0) return '+0s';
  const totalSeconds = Math.floor(diffMs / 1000);
  if (totalSeconds < 60) return `+${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) {
    return seconds > 0 ? `+${minutes}m ${seconds}s` : `+${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `+${hours}h ${remainingMinutes}m` : `+${hours}h`;
}

const LIFECYCLE_LABELS: Record<string, string> = {
  created: 'Created',
  rca_started: 'RCA Started',
  rca_completed: 'RCA Completed',
  resolved: 'Resolved',
};

function LifecycleTimeline({ events }: { events: LifecycleEvent[] }) {
  if (!events.length) return null;

  return (
    <div className="mb-6">
      <p className="text-[11px] text-zinc-500 uppercase tracking-wider mb-3">Lifecycle</p>
      <div className="flex items-center gap-0 overflow-x-auto pb-2">
        {events.map((event, i) => (
          <React.Fragment key={i}>
            <div className="flex flex-col items-center shrink-0">
              <div className="w-3 h-3 rounded-full bg-orange-500/80 border-2 border-orange-400/40" />
              <p className="text-[11px] text-zinc-300 mt-1.5 whitespace-nowrap">
                {LIFECYCLE_LABELS[event.eventType] || event.eventType}
              </p>
              <p className="text-[10px] text-zinc-600 mt-0.5 font-mono whitespace-nowrap">
                {event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : 'N/A'}
              </p>
            </div>
            {i < events.length - 1 && (
              <div className="flex-1 min-w-[40px] h-px bg-zinc-700 mx-2 mt-[-20px]" />
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function StepRow({ step, baseTime }: { step: ExecutionStep; baseTime: Date }) {
  const [expanded, setExpanded] = useState(false);
  const stepTime = step.timestamp ? new Date(step.timestamp) : null;
  const isThought = step.type === 'thought';
  const content = step.content ?? '';
  const truncatedContent = content.length > 120
    ? content.slice(0, 120) + '...'
    : content;
  const needsExpand = content.length > 120;

  return (
    <div className="flex gap-3 group">
      {/* Timeline rail */}
      <div className="flex flex-col items-center shrink-0 pt-1">
        <div className={`w-2 h-2 rounded-full ${isThought ? 'bg-blue-500' : 'bg-green-500'}`} />
        <div className="w-px flex-1 bg-zinc-800 mt-1" />
      </div>

      {/* Content */}
      <div className="pb-4 min-w-0 flex-1">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-[11px] font-mono text-zinc-600 shrink-0">
            {stepTime ? formatRelativeTime(baseTime, stepTime) : 'N/A'}
          </span>
          <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium ${
            isThought
              ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
              : 'bg-green-500/10 text-green-400 border border-green-500/20'
          }`}>
            {isThought ? <Brain className="w-3 h-3" /> : <Terminal className="w-3 h-3" />}
            {isThought ? 'thought' : 'tool_call'}
          </span>
          <span className="text-xs text-zinc-300 truncate">{step.toolName ?? '(unknown)'}</span>
        </div>

        {/* Command line for tool_calls */}
        {!isThought && step.command && (
          <div className="mt-1 px-2 py-1 rounded bg-zinc-900 border border-zinc-800">
            <code className="text-[11px] font-mono text-green-300 break-all">{step.command}</code>
          </div>
        )}

        {/* Content (collapsible) */}
        {content && (
          <div className="mt-1.5">
            {needsExpand ? (
              <button
                onClick={() => setExpanded(!expanded)}
                className="text-left w-full group/expand"
              >
                <div className="flex items-start gap-1">
                  {expanded ? (
                    <ChevronDown className="w-3 h-3 text-zinc-600 mt-0.5 shrink-0" />
                  ) : (
                    <ChevronRight className="w-3 h-3 text-zinc-600 mt-0.5 shrink-0" />
                  )}
                  <p className="text-xs text-zinc-500 break-words">
                    {expanded ? content : truncatedContent}
                  </p>
                </div>
              </button>
            ) : (
              <p className="text-xs text-zinc-500 break-words pl-4">{content}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ExecutionWaterfall({ incidentId }: { incidentId: string }) {
  const [data, setData] = useState<AgentExecutionData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchData() {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/metrics/agent-execution?incident_id=${encodeURIComponent(incidentId)}`);
        if (!res.ok) {
          throw new Error(`Failed to fetch (${res.status})`);
        }
        const json = await res.json();
        if (!cancelled) {
          setData(json);
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err.message || 'Failed to load execution data');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchData();
    return () => { cancelled = true; };
  }, [incidentId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="w-4 h-4 text-zinc-500 animate-spin" />
        <span className="text-xs text-zinc-500 ml-2">Loading execution trace...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="py-6 text-center">
        <p className="text-xs text-zinc-500">{error}</p>
      </div>
    );
  }

  if (!data || (!data.steps.length && !data.lifecycle.length)) {
    return (
      <div className="py-6 text-center">
        <p className="text-xs text-zinc-500">No execution data available</p>
      </div>
    );
  }

  // Use the first non-null timestamp from either lifecycle or steps so sparse
  // rows don't anchor the waterfall to epoch zero.
  const baseTimestamp =
    data.lifecycle.find(e => e.timestamp)?.timestamp ??
    data.steps.find(s => s.timestamp)?.timestamp;
  const baseTime = baseTimestamp ? new Date(baseTimestamp) : new Date();

  return (
    <div className="rounded-lg bg-zinc-900/50 border border-zinc-800 p-4">
      <h3 className="text-sm font-medium text-zinc-300 mb-4">Agent Execution Waterfall</h3>

      {/* Lifecycle timeline */}
      <LifecycleTimeline events={data.lifecycle} />

      {/* Steps timeline */}
      {data.steps.length > 0 && (
        <div>
          <p className="text-[11px] text-zinc-500 uppercase tracking-wider mb-3">
            Steps ({data.steps.length})
          </p>
          <div>
            {data.steps.map((step, i) => (
              <StepRow key={i} step={step} baseTime={baseTime} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
