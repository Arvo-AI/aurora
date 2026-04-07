'use client';

import { useMemo, useState } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend,
} from 'recharts';
import {
  Activity, Clock, Loader2, Zap,
  AlertTriangle, Wrench,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import type {
  Period, MetricsSummary, MttrResponse, IncidentFrequencyResponse,
  AgentExecutionResponse,
} from '@/lib/services/metrics';

const PERIODS: { label: string; value: Period }[] = [
  { label: '7 days', value: '7d' },
  { label: '30 days', value: '30d' },
  { label: '90 days', value: '90d' },
];

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444',
  high: '#f97316',
  medium: '#eab308',
  low: '#3b82f6',
  unknown: '#6b7280',
};

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'unknown'];

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return 'N/A';
  if (seconds < 0) return '0s';
  // Sub-second values get a distinct label so a real <1s MTTD doesn't get
  // rounded to "0s" and look indistinguishable from missing data.
  if (seconds > 0 && seconds < 1) return '<1s';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (mins < 60) {
    return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
  }
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  if (hours < 24) {
    return remainMins > 0 ? `${hours}h ${remainMins}m` : `${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const remainHours = hours % 24;
  return remainHours > 0 ? `${days}d ${remainHours}h` : `${days}d`;
}

// API returns YYYY-MM-DD; build a local-time Date so users in negative UTC
// offsets don't see the previous day.
function formatDate(dateStr: string): string {
  const parts = dateStr.split('-').map(Number);
  const d = parts.length === 3 && parts.every(n => Number.isFinite(n))
    ? new Date(parts[0], parts[1] - 1, parts[2])
    : new Date(dateStr);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function severityColor(severity: string): string {
  return SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.unknown;
}

function severityBgClass(severity: string): string {
  switch (severity) {
    case 'critical': return 'bg-red-500/20 text-red-400';
    case 'high': return 'bg-orange-500/20 text-orange-400';
    case 'medium': return 'bg-yellow-500/20 text-yellow-400';
    case 'low': return 'bg-blue-500/20 text-blue-400';
    default: return 'bg-zinc-500/20 text-zinc-400';
  }
}

function metricsFetcher<T>(key: string, signal: AbortSignal): Promise<T> {
  return jsonFetcher<T>(key, signal);
}

export default function MetricsPanel() {
  const [period, setPeriod] = useState<Period>('30d');

  const { data: summary, isLoading: summaryLoading, error: summaryError } = useQuery<MetricsSummary>(
    `/api/metrics/summary?period=${period}`,
    metricsFetcher,
    { staleTime: 30_000 },
  );

  const { data: mttr, isLoading: mttrLoading } = useQuery<MttrResponse>(
    `/api/metrics/mttr?period=${period}`,
    metricsFetcher,
    { staleTime: 30_000 },
  );

  const { data: frequency, isLoading: freqLoading } = useQuery<IncidentFrequencyResponse>(
    `/api/metrics/incident-frequency?period=${period}&group_by=severity`,
    metricsFetcher,
    { staleTime: 30_000 },
  );

  const { data: agentExec, isLoading: agentLoading } = useQuery<AgentExecutionResponse>(
    `/api/metrics/agent-execution?period=${period}`,
    metricsFetcher,
    { staleTime: 30_000 },
  );

  const isLoading = summaryLoading && !summary;

  const frequencySeverities = useMemo(() => {
    if (!frequency?.data) return [];
    const seen = new Set(frequency.data.map(d => d.group));
    return SEVERITY_ORDER.filter(s => seen.has(s));
  }, [frequency]);

  const frequencyChartData = useMemo(() => {
    if (!frequency?.data) return [];
    const dateMap = new Map<string, Record<string, number>>();
    // Seed every date entry with all severity buckets at 0 so Recharts treats
    // missing groups as zeros instead of "no data" gaps in the stacked area.
    const seedZeros = (): Record<string, number> =>
      Object.fromEntries(frequencySeverities.map(s => [s, 0]));
    for (const pt of frequency.data) {
      if (!dateMap.has(pt.date)) dateMap.set(pt.date, seedZeros());
      const entry = dateMap.get(pt.date)!;
      entry[pt.group] = (entry[pt.group] || 0) + pt.count;
    }
    return Array.from(dateMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, counts]) => ({ date, ...counts }));
  }, [frequency, frequencySeverities]);

  if (isLoading) {
    return (
      <div className="flex justify-center py-24">
        <Loader2 className="h-8 w-8 animate-spin text-zinc-500" />
      </div>
    );
  }

  if (summaryError && !summary) {
    return (
      <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-6 text-center">
        <AlertTriangle className="h-8 w-8 mx-auto text-red-400 mb-3" />
        <p className="text-zinc-200 font-medium">Failed to load metrics</p>
        <p className="text-zinc-400 text-sm mt-1">{summaryError.message}</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex justify-end mb-6">
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {summary && <SummaryCards summary={summary} />}

      <div className="space-y-8 mt-8">
        {mttr && <MttrSection mttr={mttr} loading={mttrLoading} />}

        <FrequencyChart
          data={frequencyChartData}
          severities={frequencySeverities}
          loading={freqLoading}
        />

        {agentExec && <AgentSection agent={agentExec} loading={agentLoading} />}
      </div>
    </div>
  );
}

function PeriodSelector({
  value,
  onChange,
}: {
  value: Period;
  onChange: (p: Period) => void;
}) {
  return (
    <div className="flex rounded-lg border border-zinc-800 overflow-hidden">
      {PERIODS.map(p => (
        <button
          key={p.value}
          onClick={() => onChange(p.value)}
          className={`px-3 py-1.5 text-sm font-medium transition-colors ${
            value === p.value
              ? 'bg-zinc-800 text-zinc-200'
              : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-900'
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}

function SummaryCards({ summary }: { summary: MetricsSummary }) {
  const cards = [
    {
      label: 'Total Incidents',
      value: summary.totalIncidents.toString(),
      sub: `${summary.activeIncidents} active`,
      icon: Activity,
      accent: 'text-zinc-200',
    },
    {
      label: 'Avg Investigation Time',
      value: formatDuration(summary.avgMttrSeconds),
      sub: 'webhook arrival to RCA verdict',
      icon: Clock,
      accent: 'text-zinc-200',
    },
    {
      label: 'Avg MTTD',
      value: formatDuration(summary.avgMttdSeconds),
      sub: 'pickup latency to start RCA',
      icon: Zap,
      accent: 'text-zinc-200',
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      {cards.map(card => (
        <div
          key={card.label}
          className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4"
        >
          <div className="flex items-center gap-2 mb-2">
            <card.icon className="h-4 w-4 text-zinc-500" />
            <span className="text-sm text-zinc-400">{card.label}</span>
          </div>
          <p className={`text-2xl font-semibold ${card.accent}`}>{card.value}</p>
          <p className="text-xs text-zinc-500 mt-1">{card.sub}</p>
        </div>
      ))}
    </div>
  );
}

function MttrSection({ mttr, loading }: { mttr: MttrResponse; loading: boolean }) {
  if (mttr.bySeverity.length === 0) return null;

  const maxMttr = Math.max(
    ...mttr.bySeverity.map(s => (s.avgDetectionToRcaSeconds ?? 0) + (s.avgRcaToResolveSeconds ?? 0)),
    1,
  );

  return (
    <section>
      <SectionHeader icon={Clock} title="MTTR by Severity" loading={loading} />
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-5">
        <div className="space-y-4">
          {mttr.bySeverity
            .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
            .map(sev => {
              const detection = sev.avgDetectionToRcaSeconds ?? 0;
              const resolve = sev.avgRcaToResolveSeconds ?? 0;
              const total = detection + resolve;
              const detectionPct = total > 0 ? (detection / total) * 100 : 0;
              const rcaPct = total > 0 ? (resolve / total) * 100 : 0;
              const barWidth = total > 0 ? (total / maxMttr) * 100 : 0;

              return (
                <div key={sev.severity}>
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-2">
                      <span
                        className={`text-xs font-medium px-2 py-0.5 rounded ${severityBgClass(sev.severity)}`}
                      >
                        {sev.severity}
                      </span>
                      <span className="text-sm text-zinc-400">
                        {sev.count} incident{sev.count !== 1 ? 's' : ''}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-xs text-zinc-500">
                      <span>p50: {formatDuration(sev.p50MttrSeconds)}</span>
                      <span>p95: {formatDuration(sev.p95MttrSeconds)}</span>
                      <span className="text-zinc-300 font-medium">
                        avg: {formatDuration(sev.avgMttrSeconds)}
                      </span>
                    </div>
                  </div>
                  <div
                    className="flex h-3 rounded-full overflow-hidden bg-zinc-800"
                    style={{ width: `${Math.max(barWidth, 4)}%` }}
                  >
                    <div
                      className="h-full"
                      style={{
                        width: `${detectionPct}%`,
                        backgroundColor: severityColor(sev.severity),
                        opacity: 0.6,
                      }}
                      title={`Detection to RCA: ${formatDuration(sev.avgDetectionToRcaSeconds)}`}
                    />
                    <div
                      className="h-full"
                      style={{
                        width: `${rcaPct}%`,
                        backgroundColor: severityColor(sev.severity),
                      }}
                      title={`RCA to Resolve: ${formatDuration(sev.avgRcaToResolveSeconds)}`}
                    />
                  </div>
                </div>
              );
            })}
        </div>
        <div className="flex items-center gap-4 mt-4 text-xs text-zinc-500">
          <span className="flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-sm bg-zinc-500 opacity-60" />
            Detection to RCA
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-sm bg-zinc-500" />
            RCA to Resolve
          </span>
        </div>
      </div>
    </section>
  );
}

function FrequencyChart({
  data,
  severities,
  loading,
}: {
  data: Record<string, any>[];
  severities: string[];
  loading: boolean;
}) {
  return (
    <section>
      <SectionHeader icon={Activity} title="Incident Frequency" loading={loading} />
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-5">
        {data.length === 0 ? (
          <p className="text-zinc-500 text-sm text-center py-8">
            No incident data for this period.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
              <XAxis
                dataKey="date"
                tickFormatter={formatDate}
                tick={{ fill: '#71717a', fontSize: 12 }}
                stroke="#3f3f46"
              />
              <YAxis
                allowDecimals={false}
                tick={{ fill: '#71717a', fontSize: 12 }}
                stroke="#3f3f46"
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#18181b',
                  border: '1px solid #3f3f46',
                  borderRadius: '0.5rem',
                  fontSize: 12,
                }}
                labelFormatter={formatDate}
                labelStyle={{ color: '#d4d4d8' }}
              />
              <Legend
                wrapperStyle={{ fontSize: 12, color: '#a1a1aa' }}
              />
              {severities.map(sev => (
                <Area
                  key={sev}
                  type="monotone"
                  dataKey={sev}
                  stackId="1"
                  stroke={severityColor(sev)}
                  fill={severityColor(sev)}
                  fillOpacity={0.3}
                  strokeWidth={1.5}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}

function AgentSection({
  agent,
  loading,
}: {
  agent: AgentExecutionResponse;
  loading: boolean;
}) {
  const sorted = [...agent.toolStats].sort((a, b) => b.totalCalls - a.totalCalls);

  return (
    <section>
      <SectionHeader icon={Zap} title="Agent Performance" loading={loading} />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
          <div className="flex items-center gap-2 mb-1">
            <Activity className="h-4 w-4 text-zinc-500" />
            <span className="text-sm text-zinc-400">Avg Steps per RCA</span>
          </div>
          <p className="text-2xl font-semibold text-zinc-200">
            {Number(agent.avgStepsPerRca ?? 0).toFixed(1)}
          </p>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
          <div className="flex items-center gap-2 mb-1">
            <Wrench className="h-4 w-4 text-zinc-500" />
            <span className="text-sm text-zinc-400">Total RCAs Completed</span>
          </div>
          <p className="text-2xl font-semibold text-zinc-200">
            {agent.totalRcasCompleted}
          </p>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
          <div className="flex items-center gap-2 mb-1">
            <Wrench className="h-4 w-4 text-zinc-500" />
            <span className="text-sm text-zinc-400">Unique Tools Used</span>
          </div>
          <p className="text-2xl font-semibold text-zinc-200">
            {agent.toolStats.length}
          </p>
        </div>
      </div>

      {sorted.length > 0 && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden mt-4">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-500 text-xs uppercase tracking-wider">
                <th className="text-left px-5 py-2 font-medium">Tool</th>
                <th className="text-right px-5 py-2 font-medium">Total Calls</th>
                <th className="text-right px-5 py-2 font-medium">Incidents Used</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map(tool => (
                <tr
                  key={tool.toolName}
                  className="border-b border-zinc-800/50 hover:bg-zinc-800/30 transition-colors"
                >
                  <td className="px-5 py-2.5 text-zinc-200 font-mono text-xs">
                    {tool.toolName}
                  </td>
                  <td className="px-5 py-2.5 text-right text-zinc-400">
                    {tool.totalCalls}
                  </td>
                  <td className="px-5 py-2.5 text-right text-zinc-400">
                    {tool.incidentsUsed}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function SectionHeader({
  icon: Icon,
  title,
  loading,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  loading: boolean;
}) {
  return (
    <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wide mb-3 flex items-center gap-2">
      <Icon className="h-4 w-4 text-zinc-500" />
      {title}
      {loading && <Loader2 className="h-3 w-3 animate-spin text-zinc-600 ml-1" />}
    </h2>
  );
}
