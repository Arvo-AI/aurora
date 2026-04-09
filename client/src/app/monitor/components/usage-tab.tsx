'use client';

import { useMemo, useState, useEffect, useCallback } from 'react';
import { DollarSign, Cpu, Zap, AlertTriangle, Hash } from 'lucide-react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend, ReferenceArea,
} from 'recharts';
import { RotateCcw, ZoomIn } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import {
  StatCard, StatCardSkeleton, ChartPanel, ChartSkeleton, EmptyState,
  GrafanaLineChart,
  formatCompact, formatCost,
  CHART_COLORS, type Period,
} from './charts';

interface UsageSummary {
  total_cost: number;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_requests: number;
  error_count: number;
  error_rate: number;
  avg_response_ms: number | null;
  models_used: number;
}

interface CostPoint {
  date: string;
  group: string;
  cost: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  request_count: number;
}

interface CostOverTimeResponse {
  data: CostPoint[];
  group_by: string;
  granularity?: 'hour' | 'day' | 'week';
}

interface ModelUsage {
  model_name: string;
  usage_count: number;
  total_cost: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
}

interface ModelsResponse {
  models: ModelUsage[];
  billing_summary: { total_api_cost: number };
}

function shortModelName(name: string): string {
  const parts = name.split('/');
  return parts[parts.length - 1];
}

function formatCostAxis(n: number): string {
  if (n >= 1000) return `$${(n / 1000).toFixed(0)}K`;
  if (n >= 1) return `$${n.toFixed(0)}`;
  if (n >= 0.01) return `$${n.toFixed(2)}`;
  return '$0';
}

function formatTokenAxis(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// Pure time formatter. The hour-granularity "day header" behavior used to live
// here as mutable closure state, but that got stale whenever the input data
// changed and produced wrong axis labels. The header decision is now made at
// the data-transform step (see makeHourAxisFormatter below), so this function
// stays a pure function of the single input.
function makeTimeFormatter(granularity: 'hour' | 'day' | 'week' = 'day') {
  return (dateStr: string): string => {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    if (granularity === 'hour') {
      return d.toLocaleString('en-US', { hour: 'numeric', hour12: true });
    }
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  };
}

// Build a pure axis formatter for hour-granularity timelines that shows the
// day label on the first tick of each new day and the hour otherwise. The
// "first of day" decision is precomputed from the data so the returned
// formatter has no mutable state and is safe across re-renders.
function makeHourAxisFormatter(dates: string[]) {
  const dayHeaderAt = new Set<string>();
  let lastDayStr = '';
  for (const ds of dates) {
    const d = new Date(ds);
    if (isNaN(d.getTime())) continue;
    const dayStr = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    if (dayStr !== lastDayStr) {
      dayHeaderAt.add(ds);
      lastDayStr = dayStr;
    }
  }
  return (dateStr: string): string => {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    if (dayHeaderAt.has(dateStr)) {
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }
    return d.toLocaleString('en-US', { hour: 'numeric', hour12: true });
  };
}

const GRID_STROKE = 'rgba(255,255,255,0.04)';
const AXIS_STROKE = 'rgba(255,255,255,0.06)';
const AXIS_TICK = { fontSize: 11, fill: '#71717a', fontFamily: 'var(--font-mono, ui-monospace, monospace)' };
const INPUT_COLOR = '#3b82f6';
const OUTPUT_COLOR = '#8b5cf6';

function formatTooltipDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  const hasTime = dateStr.includes('T') || dateStr.includes(' ');
  if (hasTime) {
    return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function DualAxisTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-zinc-700/60 bg-zinc-900/95 backdrop-blur-md px-3 py-2.5 shadow-xl">
      <p className="text-[11px] text-zinc-500 mb-1.5">{formatTooltipDate(label)}</p>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="flex items-center justify-between gap-6 text-[12px]">
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full" style={{ background: p.color }} />
            <span className="text-zinc-400">{p.name}</span>
          </span>
          <span className="text-zinc-100 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>
            {formatCompact(p.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function DualAxisTokenChart({ data, timeFormat }: { data: Array<{ date: string; input: number; output: number }>; timeFormat: (s: string) => string }) {
  const [refLeft, setRefLeft] = useState('');
  const [refRight, setRefRight] = useState('');
  const [startIdx, setStartIdx] = useState(0);
  const [endIdx, setEndIdx] = useState(data.length - 1);
  const [zoomed, setZoomed] = useState(false);

  // Reset / clamp zoom state when the data prop changes so that sliced()
  // never indexes out of bounds after a period or filter switch.
  useEffect(() => {
    setStartIdx(0);
    setEndIdx(Math.max(0, data.length - 1));
    setZoomed(false);
    setRefLeft('');
    setRefRight('');
  }, [data.length]);

  const handleMouseDown = useCallback((e: any) => {
    if (e?.activeLabel) setRefLeft(e.activeLabel);
  }, []);

  const handleMouseMove = useCallback((e: any) => {
    if (refLeft && e?.activeLabel) setRefRight(e.activeLabel);
  }, [refLeft]);

  const handleMouseUp = useCallback(() => {
    if (refLeft && refRight && refLeft !== refRight) {
      const li = data.findIndex(d => d.date === refLeft);
      const ri = data.findIndex(d => d.date === refRight);
      if (li >= 0 && ri >= 0) {
        setStartIdx(Math.min(li, ri));
        setEndIdx(Math.max(li, ri));
        setZoomed(true);
      }
    }
    setRefLeft('');
    setRefRight('');
  }, [data, refLeft, refRight]);

  const handleReset = useCallback(() => {
    setStartIdx(0);
    setEndIdx(data.length - 1);
    setZoomed(false);
  }, [data.length]);

  const sliced = zoomed ? data.slice(startIdx, endIdx + 1) : data;

  return (
    <div className="relative">
      {zoomed && (
        <button
          onClick={handleReset}
          className="absolute top-0 right-0 z-10 flex items-center gap-1 text-[11px] text-zinc-400 bg-zinc-800/80 border border-zinc-700/50 rounded-md px-2 py-1 hover:text-zinc-200 transition-colors"
        >
          <RotateCcw className="h-3 w-3" /> Reset zoom
        </button>
      )}
      <ResponsiveContainer width="100%" height={300}>
        <AreaChart
          data={sliced}
          margin={{ top: 16, right: 8, bottom: 0, left: 8 }}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
        >
          <defs>
            <linearGradient id="inputGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={INPUT_COLOR} stopOpacity={0.2} />
              <stop offset="100%" stopColor={INPUT_COLOR} stopOpacity={0} />
            </linearGradient>
            <linearGradient id="outputGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={OUTPUT_COLOR} stopOpacity={0.2} />
              <stop offset="100%" stopColor={OUTPUT_COLOR} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_TICK}
            stroke={AXIS_STROKE}
            tickLine={false}
            axisLine={false}
            tickFormatter={timeFormat}
            minTickGap={40}
          />
          <YAxis
            yAxisId="left"
            tick={{ ...AXIS_TICK, fill: INPUT_COLOR + '99' }}
            stroke="transparent"
            tickLine={false}
            axisLine={false}
            tickFormatter={formatTokenAxis}
            width={56}
            orientation="left"
          />
          <YAxis
            yAxisId="right"
            tick={{ ...AXIS_TICK, fill: OUTPUT_COLOR + '99' }}
            stroke="transparent"
            tickLine={false}
            axisLine={false}
            tickFormatter={formatTokenAxis}
            width={56}
            orientation="right"
          />
          <Tooltip content={<DualAxisTooltip />} />
          <Area
            yAxisId="left"
            type="monotoneX"
            dataKey="input"
            name="Input Tokens"
            stroke={INPUT_COLOR}
            strokeWidth={2}
            fill="url(#inputGrad)"
            animationDuration={800}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 2, fill: '#09090b' }}
          />
          <Area
            yAxisId="right"
            type="monotoneX"
            dataKey="output"
            name="Output Tokens"
            stroke={OUTPUT_COLOR}
            strokeWidth={2}
            fill="url(#outputGrad)"
            animationDuration={800}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 2, fill: '#09090b' }}
          />
          {refLeft && refRight && refLeft !== refRight && (
            <ReferenceArea yAxisId="left" x1={refLeft} x2={refRight} stroke="#ffffff20" fill="#ffffff08" />
          )}
          <Legend
            verticalAlign="bottom"
            height={28}
            iconType="circle"
            iconSize={8}
            wrapperStyle={{ fontSize: 11, color: '#a1a1aa', paddingTop: 8 }}
          />
        </AreaChart>
      </ResponsiveContainer>
      {!zoomed && (
        <div className="flex items-center justify-end mt-1 text-[10px] text-zinc-600 gap-1">
          <ZoomIn className="h-3 w-3" /> Drag to zoom
        </div>
      )}
    </div>
  );
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export default function UsageTab({ period }: { period: Period }) {
  const { data: summary, isLoading: summaryLoading } = useQuery<UsageSummary>(
    `/api/llm-usage/summary?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: costData, isLoading: costLoading } = useQuery<CostOverTimeResponse>(
    `/api/llm-usage/cost-over-time?period=${period}&group_by=model`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: modelsData, isLoading: modelsLoading } = useQuery<ModelsResponse>(
    `/api/llm-usage/models`,
    jsonFetcher,
    { staleTime: 60_000 },
  );

  const granularity = costData?.granularity ?? 'day';

  const { costChartData, costSeries } = useMemo(() => {
    if (!costData?.data?.length) return { costChartData: [], costSeries: [] };

    // Aggregate cost per short model name so we can pick the top-N by spend
    // rather than by arbitrary first-appearance order.
    const modelCosts = new Map<string, number>();
    const dateMap = new Map<string, Record<string, number>>();

    for (const pt of costData.data) {
      const short = shortModelName(pt.group);
      modelCosts.set(short, (modelCosts.get(short) ?? 0) + pt.cost);
      if (!dateMap.has(pt.date)) dateMap.set(pt.date, {});
      const entry = dateMap.get(pt.date)!;
      entry[short] = (entry[short] || 0) + pt.cost;
    }

    const allGroups = Array.from(modelCosts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 7)
      .map(([name]) => name);

    const series = allGroups.map((g, i) => ({
      key: g,
      name: g,
      color: CHART_COLORS[i % CHART_COLORS.length],
      stacked: false,
    }));

    const zeros = Object.fromEntries(allGroups.map(g => [g, 0]));

    const data = Array.from(dateMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, vals]) => ({ date, ...zeros, ...vals }));

    return { costChartData: data, costSeries: series };
  }, [costData]);

  // Time formatter depends on both granularity and the actual dates in the
  // chart so the "day header on first tick of each day" behavior stays in
  // sync when the data changes.
  const timeFormat = useMemo(() => {
    if (granularity === 'hour') {
      return makeHourAxisFormatter(costChartData.map(d => d.date as string));
    }
    return makeTimeFormatter(granularity);
  }, [granularity, costChartData]);

  const tokenChartData = useMemo(() => {
    if (!costData?.data?.length) return [];
    const dateMap = new Map<string, { input: number; output: number }>();
    for (const pt of costData.data) {
      if (!dateMap.has(pt.date)) dateMap.set(pt.date, { input: 0, output: 0 });
      const entry = dateMap.get(pt.date)!;
      entry.input += pt.input_tokens;
      entry.output += pt.output_tokens;
    }
    return Array.from(dateMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, vals]) => ({ date, ...vals }));
  }, [costData]);

  const sortedModels = useMemo(() => {
    if (!modelsData?.models) return [];
    return [...modelsData.models].sort((a, b) => b.total_cost - a.total_cost);
  }, [modelsData]);

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {summaryLoading ? (
          Array.from({ length: 4 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : summary ? (
          <>
            <StatCard label="Total Cost" value={formatCost(summary.total_cost)} icon={DollarSign} sub={`${period} period`} />
            <StatCard label="Total Tokens" value={formatCompact(summary.total_tokens)} icon={Hash} sub={`${formatCompact(summary.total_input_tokens)} in / ${formatCompact(summary.total_output_tokens)} out`} />
            <StatCard label="Requests" value={formatCompact(summary.total_requests)} icon={Zap} sub={summary.avg_response_ms ? `avg ${summary.avg_response_ms}ms` : undefined} />
            <StatCard label="Error Rate" value={`${summary.error_rate}%`} icon={AlertTriangle} sub={`${summary.error_count} errors`} />
          </>
        ) : null}
      </div>

      {/* Cost over time -- lines, not stacked areas */}
      <ChartPanel title="Cost Over Time" subtitle={`${granularity === 'hour' ? 'Hourly' : granularity === 'week' ? 'Weekly' : 'Daily'} spend by model`} loading={costLoading}>
        {costLoading ? (
          <ChartSkeleton />
        ) : !costChartData.length ? (
          <EmptyState icon={DollarSign} message="No cost data for this period" />
        ) : (
          <GrafanaLineChart
            data={costChartData}
            series={costSeries.map(s => ({ key: s.key, name: s.name, color: s.color }))}
            xFormatter={timeFormat}
            yFormatter={formatCostAxis}
            tooltipFormatter={(v) => formatCost(v)}
            yAxisWidth={56}
          />
        )}
      </ChartPanel>

      {/* Token usage -- dual Y-axis so input and output are both readable */}
      <ChartPanel title="Token Usage" subtitle={`Input vs output tokens · ${granularity === 'hour' ? 'hourly' : granularity === 'week' ? 'weekly' : 'daily'} · dual scale`} loading={costLoading}>
        {costLoading ? (
          <ChartSkeleton />
        ) : !tokenChartData.length ? (
          <EmptyState icon={Cpu} message="No token data for this period" />
        ) : (
          <DualAxisTokenChart data={tokenChartData} timeFormat={timeFormat} />
        )}
      </ChartPanel>

      {/* Model breakdown table */}
      <ChartPanel title="Model Breakdown" loading={modelsLoading}>
        {!sortedModels.length ? (
          <EmptyState icon={Cpu} message="No models used yet" />
        ) : (
          <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800/60 bg-zinc-900/80">
                  <th className="text-left px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Model</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Requests</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Tokens</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Cost</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Share</th>
                </tr>
              </thead>
              <tbody>
                {sortedModels.map((m) => {
                  const totalCost = modelsData!.billing_summary.total_api_cost || 1;
                  const share = (m.total_cost / totalCost) * 100;
                  return (
                    <tr key={m.model_name} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150">
                      <td className="px-4 py-2.5 text-zinc-200 font-mono text-xs">{shortModelName(m.model_name)}</td>
                      <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{m.usage_count.toLocaleString()}</td>
                      <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatCompact(m.total_tokens)}</td>
                      <td className="px-4 py-2.5 text-right text-zinc-200 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatCost(m.total_cost)}</td>
                      <td className="px-4 py-2.5 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-16 h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                            <div className="h-full rounded-full bg-blue-500/60" style={{ width: `${Math.min(share, 100)}%` }} />
                          </div>
                          <span className="text-xs text-zinc-500 w-10 text-right" style={{ fontVariantNumeric: 'tabular-nums' }}>{share.toFixed(1)}%</span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </ChartPanel>
    </div>
  );
}
