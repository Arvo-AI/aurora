'use client';

import { useMemo } from 'react';
import type { SubAgentRun, SubAgentRunStatus } from '@/lib/services/incidents';

// Matches the shape of ExecutionStep produced by /api/metrics/agent-execution
// (kept local because the upstream interface is not exported).
export interface ExecutionStep {
  type: 'thought' | 'tool_call';
  timestamp: string | null;
  toolName: string | null;
  command: string | null;
  content: string | null;
  agentId?: string | null;
  endTimestamp?: string | null;
  status?: SubAgentRunStatus | 'completed' | 'error' | null;
}

interface AgentSwimlaneProps {
  runs: SubAgentRun[];
  executionSteps: ExecutionStep[];
}

interface TimeWindow {
  startMs: number;
  endMs: number;
}

function statusFill(status: ExecutionStep['status']): string {
  switch (status) {
    case 'failed':
    case 'error':
      return 'bg-red-500/70 border-red-400';
    case 'cancelled':
      return 'bg-zinc-500/60 border-zinc-400';
    case 'running':
      return 'bg-amber-500/70 border-amber-400';
    case 'succeeded':
    case 'completed':
      return 'bg-green-500/70 border-green-400';
    default:
      return 'bg-blue-500/60 border-blue-400';
  }
}

function computeWindow(runs: SubAgentRun[], steps: ExecutionStep[]): TimeWindow | null {
  const times: number[] = [];
  for (const r of runs) {
    if (r.started_at) times.push(new Date(r.started_at).getTime());
    if (r.ended_at) times.push(new Date(r.ended_at).getTime());
  }
  for (const s of steps) {
    if (s.timestamp) times.push(new Date(s.timestamp).getTime());
    if (s.endTimestamp) times.push(new Date(s.endTimestamp).getTime());
  }
  if (!times.length) return null;
  const startMs = Math.min(...times);
  const endMs = Math.max(...times);
  return { startMs, endMs: endMs === startMs ? startMs + 1000 : endMs };
}

function formatTickLabel(ms: number, baseMs: number): string {
  const offsetSec = Math.floor((ms - baseMs) / 1000);
  if (offsetSec < 60) return `+${offsetSec}s`;
  const min = Math.floor(offsetSec / 60);
  const sec = offsetSec % 60;
  return sec ? `+${min}m${sec}s` : `+${min}m`;
}

export default function AgentSwimlane({ runs, executionSteps }: AgentSwimlaneProps) {
  const timeWindow = useMemo(() => computeWindow(runs, executionSteps), [runs, executionSteps]);

  const stepsByAgent = useMemo(() => {
    const map = new Map<string, ExecutionStep[]>();
    const fallbackKey = runs.find((r) => r.role === 'main')?.agent_id ?? '__main__';
    for (const step of executionSteps) {
      const key = step.agentId ?? fallbackKey;
      const list = map.get(key) ?? [];
      list.push(step);
      map.set(key, list);
    }
    return map;
  }, [executionSteps, runs]);

  if (!runs.length) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-4 text-xs text-zinc-500">
        No agents to display.
      </div>
    );
  }

  if (!timeWindow) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-4 text-xs text-zinc-500">
        Agent timing not yet available.
      </div>
    );
  }

  const totalMs = timeWindow.endMs - timeWindow.startMs;
  const tickCount = 5;
  const ticks = Array.from({ length: tickCount + 1 }, (_, i) => {
    const ms = timeWindow.startMs + (totalMs * i) / tickCount;
    return { pct: (i / tickCount) * 100, ms };
  });

  const pct = (ms: number) => ((ms - timeWindow.startMs) / totalMs) * 100;

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3">
      {/* Time axis */}
      <div className="relative h-5 ml-40 mr-2 border-b border-zinc-800">
        {ticks.map((t, i) => (
          <div
            key={i}
            className="absolute top-0 h-full text-[10px] font-mono text-zinc-500"
            style={{ left: `${t.pct}%`, transform: 'translateX(-50%)' }}
          >
            {formatTickLabel(t.ms, timeWindow.startMs)}
          </div>
        ))}
      </div>

      <div className="mt-2 space-y-1.5">
        {runs.map((run) => {
          const startMs = run.started_at ? new Date(run.started_at).getTime() : timeWindow.startMs;
          const endMs = run.ended_at ? new Date(run.ended_at).getTime() : timeWindow.endMs;
          const left = pct(startMs);
          const width = Math.max(0.5, pct(endMs) - left);
          const steps = stepsByAgent.get(run.agent_id) ?? [];

          return (
            <div key={run.agent_id} className="flex items-center gap-2">
              <div className="w-40 shrink-0 truncate text-xs text-zinc-300 pr-2">
                {run.ui_label || run.agent_id}
              </div>
              <div className="relative flex-1 h-6 rounded bg-zinc-900 border border-zinc-800">
                <div
                  className={`absolute top-1 bottom-1 rounded ${statusFill(run.status)} border opacity-50`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={`${run.ui_label || run.agent_id}: ${run.status}`}
                />
                {steps.map((step, idx) => {
                  if (!step.timestamp) return null;
                  const sStart = new Date(step.timestamp).getTime();
                  const sEnd = step.endTimestamp ? new Date(step.endTimestamp).getTime() : sStart + 1500;
                  const sLeft = pct(sStart);
                  const sWidth = Math.max(0.4, pct(sEnd) - sLeft);
                  return (
                    <div
                      key={`${step.timestamp}-${idx}`}
                      className={`absolute top-1.5 bottom-1.5 rounded ${statusFill(step.status)} border`}
                      style={{ left: `${sLeft}%`, width: `${sWidth}%` }}
                      title={`${step.toolName ?? step.type}${step.command ? `: ${step.command}` : ''}`}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
