'use client';

import { useMemo } from 'react';
import {
  Timer, Wrench, CheckCircle2, XCircle, Clock, DollarSign,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import {
  StatCard, StatCardSkeleton, ChartPanel, ChartSkeleton, EmptyState,
  formatDuration, formatCompact, formatCost,
  CHART_COLORS, type Period,
} from './charts';

interface ToolStat {
  tool_name: string;
  call_count: number;
  incident_count: number;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  error_count: number;
  success_rate: number;
}

interface RcaSummary {
  total_rcas: number;
  successful_rcas: number;
  failed_rcas: number;
  avg_tool_calls_per_rca: number | null;
  avg_thoughts_per_rca: number | null;
  avg_rca_duration_seconds: number | null;
  avg_tokens_per_rca: number | null;
  avg_cost_per_rca: number | null;
  avg_tool_duration_ms: number | null;
}

interface ToolStatsResponse {
  tools: ToolStat[];
  rca_summary: RcaSummary;
}

export default function WaterfallTab({ period }: { period: Period }) {
  const timeRange = period;
  const { data, isLoading } = useQuery<ToolStatsResponse>(
    `/api/monitor/tools/stats?time_range=${timeRange}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const rca = data?.rca_summary;
  const tools = data?.tools;

  const sortedTools = useMemo(() => {
    if (!tools) return [];
    return [...tools].sort((a, b) => b.call_count - a.call_count);
  }, [tools]);

  const maxCalls = useMemo(() => {
    if (!sortedTools.length) return 1;
    return Math.max(...sortedTools.map(t => t.call_count), 1);
  }, [sortedTools]);

  const successRate = rca && rca.total_rcas > 0
    ? ((rca.successful_rcas / rca.total_rcas) * 100).toFixed(1)
    : '—';

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {isLoading ? (
          Array.from({ length: 4 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : rca ? (
          <>
            <StatCard label="Total RCAs" value={String(rca.total_rcas)} icon={Timer} sub={`${rca.failed_rcas} failed`} />
            <StatCard label="Success Rate" value={`${successRate}%`} icon={CheckCircle2} />
            <StatCard label="Avg Duration" value={formatDuration(rca.avg_rca_duration_seconds)} icon={Clock} sub={rca.avg_tool_calls_per_rca ? `~${Math.round(rca.avg_tool_calls_per_rca)} tool calls` : undefined} />
            <StatCard label="Avg Cost / RCA" value={rca.avg_cost_per_rca ? formatCost(rca.avg_cost_per_rca) : '—'} icon={DollarSign} sub={rca.avg_tokens_per_rca ? `~${formatCompact(rca.avg_tokens_per_rca)} tokens` : undefined} />
          </>
        ) : null}
      </div>

      {/* Tool usage distribution -- custom bar chart */}
      <ChartPanel title="Tool Usage Distribution" subtitle="Calls by tool across all RCAs" loading={isLoading}>
        {isLoading ? (
          <ChartSkeleton height={300} />
        ) : !sortedTools.length ? (
          <EmptyState icon={Wrench} message="No tool execution data" hint="Tool stats populate as Aurora investigates incidents" />
        ) : (
          <div className="space-y-2">
            {sortedTools.slice(0, 15).map((t, i) => {
              const pct = (t.call_count / maxCalls) * 100;
              const color = CHART_COLORS[i % CHART_COLORS.length];
              return (
                <div key={t.tool_name} className="group flex items-center gap-3">
                  <span className="text-xs text-zinc-400 font-mono w-40 truncate shrink-0 text-right" title={t.tool_name}>
                    {t.tool_name}
                  </span>
                  <div className="flex-1 h-6 bg-zinc-800/40 rounded overflow-hidden relative">
                    <div
                      className="h-full rounded transition-all duration-500 ease-out"
                      style={{
                        width: `${Math.max(pct, 2)}%`,
                        background: `linear-gradient(90deg, ${color}cc, ${color}80)`,
                      }}
                    />
                  </div>
                  <span className="text-xs text-zinc-400 w-12 text-right shrink-0" style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {t.call_count}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </ChartPanel>

      {/* Tool performance table */}
      <ChartPanel title="Tool Performance" loading={isLoading}>
        {!sortedTools.length ? (
          <EmptyState icon={Wrench} message="No tool performance data" />
        ) : (
          <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800/60 bg-zinc-900/80">
                  <th className="text-left px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Tool</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Calls</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Incidents</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Avg Duration</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">p95</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Success Rate</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Errors</th>
                </tr>
              </thead>
              <tbody>
                {sortedTools.map(t => (
                  <tr key={t.tool_name} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150">
                    <td className="px-4 py-2.5 text-zinc-200 font-mono text-xs">{t.tool_name}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-300 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.call_count}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.incident_count}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.avg_duration_ms != null ? `${Math.round(t.avg_duration_ms)}ms` : '—'}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.p95_duration_ms != null ? `${Math.round(t.p95_duration_ms)}ms` : '—'}</td>
                    <td className="px-4 py-2.5 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <div className="w-14 h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all duration-300 ${t.success_rate >= 95 ? 'bg-emerald-500/70' : t.success_rate >= 80 ? 'bg-yellow-500/70' : 'bg-red-500/70'}`}
                            style={{ width: `${Math.min(t.success_rate, 100)}%` }}
                          />
                        </div>
                        <span className="text-xs text-zinc-300 w-12 text-right" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.success_rate}%</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {t.error_count > 0 ? (
                        <span className="inline-flex items-center gap-1 text-red-400 text-xs font-medium">
                          <XCircle className="h-3 w-3" /> {t.error_count}
                        </span>
                      ) : (
                        <span className="text-zinc-600 text-xs">0</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </ChartPanel>
    </div>
  );
}
