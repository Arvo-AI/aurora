'use client';

import { useMemo } from 'react';
import { Activity, Clock, Zap, TrendingDown, AlertTriangle, Radio, Timer, ShieldCheck, Brain } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import type {
  MetricsSummary, MttrResponse, MttsResponse, IncidentFrequencyResponse,
  ChangeFailureRateResponse, AgentExecutionResponse,
} from '@/lib/services/metrics';
import {
  StatCard, StatCardSkeleton, ChartPanel, ChartSkeleton, EmptyState,
  GrafanaAreaChart, GrafanaBarChart, GrafanaLineChart,
  formatDuration, formatDate, formatCompact,
  SEVERITY_COLORS, CHART_COLORS, type Period,
} from './charts';

interface MttdBySeverity {
  sourceType: string;
  count: number;
  avgMttdSeconds: number | null;
  p50MttdSeconds: number | null;
  p95MttdSeconds: number | null;
}
interface MttdResponse {
  bySource: MttdBySeverity[];
}

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'unknown'];

function formatDurAxis(n: number): string {
  if (n >= 86400) return `${(n / 86400).toFixed(0)}d`;
  if (n >= 3600) return `${(n / 3600).toFixed(0)}h`;
  if (n >= 60) return `${(n / 60).toFixed(0)}m`;
  return `${n}s`;
}

export default function SreTab({ period }: { period: Period }) {
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useQuery<MetricsSummary>(
    `/api/metrics/summary?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: mttr, isLoading: mttrLoading } = useQuery<MttrResponse>(
    `/api/metrics/mttr?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: mtts, isLoading: mttsLoading } = useQuery<MttsResponse>(
    `/api/metrics/mtts?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: mttd, isLoading: mttdLoading } = useQuery<MttdResponse>(
    `/api/metrics/mttd?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: frequency, isLoading: freqLoading } = useQuery<IncidentFrequencyResponse>(
    `/api/metrics/incident-frequency?period=${period}&group_by=severity`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: cfr, isLoading: cfrLoading } = useQuery<ChangeFailureRateResponse>(
    `/api/metrics/change-failure-rate?period=${period}&window_hours=4`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  const { data: agentExec, isLoading: agentLoading } = useQuery<AgentExecutionResponse>(
    `/api/metrics/agent-execution?period=${period}`,
    jsonFetcher,
    { staleTime: 30_000 },
  );

  // MTTR trend line data (human-resolved only)
  const mttrTrendData = useMemo(() => {
    if (!mttr?.trend?.length) return [];
    return mttr.trend
      .filter(t => t.avgMttrSeconds != null)
      .map(t => ({
        date: t.date,
        MTTR: Math.round(t.avgMttrSeconds ?? 0),
        count: t.count,
      }));
  }, [mttr]);

  // MTTS trend line data (Aurora analysis time)
  const mttsTrendData = useMemo(() => {
    if (!mtts?.trend?.length) return [];
    return mtts.trend
      .filter(t => t.avgMttsSeconds != null)
      .map(t => ({
        date: t.date,
        MTTS: Math.round(t.avgMttsSeconds ?? 0),
        count: t.count,
      }));
  }, [mtts]);

  // Pivot frequency data into stacked area chart format
  const { freqChartData, freqSeries } = useMemo(() => {
    if (!frequency?.data) return { freqChartData: [], freqSeries: [] };
    const seen = new Set(frequency.data.map(d => d.group));
    const severities = SEVERITY_ORDER.filter(s => seen.has(s));
    const dateMap = new Map<string, Record<string, number>>();
    const seedZeros = (): Record<string, number> => Object.fromEntries(severities.map(s => [s, 0]));
    for (const pt of frequency.data) {
      if (!dateMap.has(pt.date)) dateMap.set(pt.date, seedZeros());
      const entry = dateMap.get(pt.date)!;
      entry[pt.group] = (entry[pt.group] || 0) + pt.count;
    }
    const data = Array.from(dateMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, counts]) => ({ date, ...counts }));
    const series = severities.map(s => ({
      key: s,
      name: s.charAt(0).toUpperCase() + s.slice(1),
      color: SEVERITY_COLORS[s] || SEVERITY_COLORS.unknown,
    }));
    return { freqChartData: data, freqSeries: series };
  }, [frequency]);

  // MTTR bar chart data (human-resolved only)
  const mttrBarData = useMemo(() => {
    if (!mttr?.bySeverity?.length) return [];
    return mttr.bySeverity
      .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
      .map(s => ({
        name: s.severity.charAt(0).toUpperCase() + s.severity.slice(1),
        'Detection → RCA': Math.round(s.avgDetectionToRcaSeconds ?? 0),
        'RCA → Resolve': Math.round(s.avgRcaToResolveSeconds ?? 0),
      }));
  }, [mttr]);

  // MTTS bar chart data (Aurora solution time by severity)
  const mttsBarData = useMemo(() => {
    if (!mtts?.bySeverity?.length) return [];
    return mtts.bySeverity
      .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
      .map(s => ({
        name: s.severity.charAt(0).toUpperCase() + s.severity.slice(1),
        'Avg MTTS': Math.round(s.avgMttsSeconds ?? 0),
        'p50': Math.round(s.p50MttsSeconds ?? 0),
        'p95': Math.round(s.p95MttsSeconds ?? 0),
      }));
  }, [mtts]);

  // MTTD bar chart data
  const mttdBarData = useMemo(() => {
    if (!mttd?.bySource?.length) return [];
    return mttd.bySource
      .filter(s => s.avgMttdSeconds != null)
      .sort((a, b) => (b.avgMttdSeconds ?? 0) - (a.avgMttdSeconds ?? 0))
      .map(s => ({
        name: s.sourceType,
        avg: Math.round(s.avgMttdSeconds ?? 0),
        p50: Math.round(s.p50MttdSeconds ?? 0),
        p95: Math.round(s.p95MttdSeconds ?? 0),
      }));
  }, [mttd]);

  // CFR bar chart data
  const cfrBarData = useMemo(() => {
    if (!cfr?.byService?.length) return [];
    return [...cfr.byService]
      .sort((a, b) => b.rate - a.rate)
      .slice(0, 10)
      .map(s => ({
        name: s.service,
        Deployments: s.totalDeployments,
        Failures: s.failureLinked,
      }));
  }, [cfr]);

  // MTTR by severity table (human-resolved only)
  const mttrTableData = mttr?.bySeverity?.length
    ? [...mttr.bySeverity].sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
    : [];

  // MTTS by severity table
  const mttsTableData = mtts?.bySeverity?.length
    ? [...mtts.bySeverity].sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity))
    : [];

  if (summaryError && !summary) {
    return (
      <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-8 text-center">
        <AlertTriangle className="h-8 w-8 mx-auto text-red-400/80 mb-3" />
        <p className="text-zinc-300 font-medium">Failed to load metrics</p>
        <p className="text-zinc-500 text-sm mt-1">{summaryError.message}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {summaryLoading ? (
          Array.from({ length: 5 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : summary ? (
          <>
            <StatCard label="Total Incidents" value={String(summary.totalIncidents)} icon={Activity} sub={`${summary.activeIncidents} active`} />
            <StatCard label="MTTS" value={formatDuration(summary.avgMttsSeconds)} icon={Brain} sub="Aurora solution time" />
            <StatCard label="MTTR" value={summary.avgMttrSeconds ? formatDuration(summary.avgMttrSeconds) : '—'} icon={Timer} sub={summary.avgMttrSeconds ? 'human resolution time' : 'no resolved incidents yet'} />
            <StatCard label="MTTD" value={formatDuration(summary.avgMttdSeconds)} icon={Zap} sub="mean time to detect" />
            <StatCard
              label="Change Failure Rate"
              value={`${Number(summary.changeFailureRate ?? 0).toFixed(1)}%`}
              icon={TrendingDown}
              sub={`${summary.totalDeployments} deployments`}
            />
          </>
        ) : null}
      </div>

      {/* MTTS Trend -- Aurora solution time (primary metric with real data) */}
      <ChartPanel title="MTTS Trend" subtitle="Mean time to solution — how fast Aurora produces an RCA" loading={mttsLoading}>
        {mttsLoading ? (
          <ChartSkeleton />
        ) : !mttsTrendData.length ? (
          <EmptyState icon={Brain} message="No analyzed incidents in this period" />
        ) : (
          <GrafanaLineChart
            data={mttsTrendData}
            series={[
              { key: 'MTTS', name: 'MTTS (seconds)', color: '#3b82f6' },
            ]}
            yFormatter={formatDurAxis}
            tooltipFormatter={(v) => formatDuration(v)}
          />
        )}
      </ChartPanel>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* MTTS by Severity */}
        <ChartPanel title="MTTS by Severity" subtitle="Aurora solution time by severity · avg / p50 / p95" loading={mttsLoading}>
          {mttsLoading ? (
            <ChartSkeleton height={220} />
          ) : !mttsBarData.length ? (
            <EmptyState icon={Brain} message="No MTTS data available" />
          ) : (
            <GrafanaBarChart
              data={mttsBarData}
              series={[
                { key: 'Avg MTTS', name: 'Average', color: '#3b82f6' },
                { key: 'p95', name: 'p95', color: '#6366f1' },
              ]}
              height={220}
              yFormatter={formatDurAxis}
              tooltipFormatter={(v) => formatDuration(v)}
            />
          )}
        </ChartPanel>

        {/* MTTD by Source */}
        <ChartPanel title="MTTD by Alert Source" subtitle="Detection latency by integration" loading={mttdLoading}>
          {mttdLoading ? (
            <ChartSkeleton height={220} />
          ) : !mttdBarData.length ? (
            <EmptyState icon={Radio} message="No MTTD data available" hint="MTTD requires alert_fired_at from your alerting provider" />
          ) : (
            <GrafanaBarChart
              data={mttdBarData}
              series={[
                { key: 'avg', name: 'Avg MTTD', color: '#06b6d4' },
                { key: 'p95', name: 'p95 MTTD', color: '#06b6d4' },
              ]}
              height={220}
              layout="vertical"
              yFormatter={formatDurAxis}
              tooltipFormatter={(v) => formatDuration(v)}
            />
          )}
        </ChartPanel>
      </div>

      {/* MTTS detail table */}
      {mttsTableData.length > 0 && (
        <ChartPanel title="MTTS Detail by Severity" subtitle="Aurora solution time breakdown" loading={mttsLoading}>
          <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800/60 bg-zinc-900/80">
                  <th className="text-left px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Severity</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Analyzed</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Avg MTTS</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">p50</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">p95</th>
                </tr>
              </thead>
              <tbody>
                {mttsTableData.map(s => (
                  <tr key={s.severity} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150">
                    <td className="px-4 py-2.5">
                      <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                        s.severity === 'critical' ? 'bg-red-500/15 text-red-400' :
                        s.severity === 'high' ? 'bg-orange-500/15 text-orange-400' :
                        s.severity === 'medium' ? 'bg-yellow-500/15 text-yellow-400' :
                        s.severity === 'low' ? 'bg-blue-500/15 text-blue-400' :
                        'bg-zinc-500/15 text-zinc-400'
                      }`}>
                        {s.severity}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right text-zinc-300 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{s.count}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-200 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.avgMttsSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.p50MttsSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.p95MttsSeconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ChartPanel>
      )}

      {/* MTTR section -- only shows when incidents have been manually resolved */}
      <ChartPanel title="MTTR Trend" subtitle="Human resolution time — populates when incidents are manually marked resolved" loading={mttrLoading}>
        {mttrLoading ? (
          <ChartSkeleton />
        ) : !mttrTrendData.length ? (
          <EmptyState icon={Timer} message="No manually resolved incidents yet" hint="MTTR tracks time from incident start to when a human marks it resolved" />
        ) : (
          <GrafanaLineChart
            data={mttrTrendData}
            series={[
              { key: 'MTTR', name: 'MTTR (seconds)', color: '#8b5cf6' },
            ]}
            yFormatter={formatDurAxis}
            tooltipFormatter={(v) => formatDuration(v)}
          />
        )}
      </ChartPanel>

      {/* MTTR detail table (only if data exists) */}
      {mttrTableData.length > 0 && (
        <ChartPanel title="MTTR Detail by Severity" subtitle="Human resolution time breakdown" loading={mttrLoading}>
          <div className="overflow-x-auto rounded-lg border border-zinc-800/60">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800/60 bg-zinc-900/80">
                  <th className="text-left px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Severity</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Resolved</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Avg MTTR</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">p50</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">p95</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">Detect → RCA</th>
                  <th className="text-right px-4 py-2.5 text-zinc-500 text-xs uppercase tracking-wider font-medium">RCA → Resolve</th>
                </tr>
              </thead>
              <tbody>
                {mttrTableData.map(s => (
                  <tr key={s.severity} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150">
                    <td className="px-4 py-2.5">
                      <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                        s.severity === 'critical' ? 'bg-red-500/15 text-red-400' :
                        s.severity === 'high' ? 'bg-orange-500/15 text-orange-400' :
                        s.severity === 'medium' ? 'bg-yellow-500/15 text-yellow-400' :
                        s.severity === 'low' ? 'bg-blue-500/15 text-blue-400' :
                        'bg-zinc-500/15 text-zinc-400'
                      }`}>
                        {s.severity}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right text-zinc-300 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{s.count}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-200 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.avgMttrSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.p50MttrSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.p95MttrSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.avgDetectionToRcaSeconds)}</td>
                    <td className="px-4 py-2.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatDuration(s.avgRcaToResolveSeconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ChartPanel>
      )}

      {/* Incident Frequency */}
      <ChartPanel title="Incident Frequency" subtitle="Incidents over time by severity" loading={freqLoading}>
        {freqLoading ? (
          <ChartSkeleton />
        ) : !freqChartData.length ? (
          <EmptyState icon={Activity} message="No incident data for this period" />
        ) : (
          <GrafanaAreaChart
            data={freqChartData}
            series={freqSeries}
          />
        )}
      </ChartPanel>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Change Failure Rate */}
        <ChartPanel title="Change Failure Rate" subtitle={cfr ? `${cfr.totalDeployments} deployments, ${Number(cfr.changeFailureRate ?? 0).toFixed(1)}% failure rate` : undefined} loading={cfrLoading}>
          {cfrLoading ? (
            <ChartSkeleton height={220} />
          ) : !cfrBarData.length ? (
            <EmptyState icon={TrendingDown} message="No deployment data" hint="Connect a CI/CD source to track failure rates" />
          ) : (
            <GrafanaBarChart
              data={cfrBarData}
              series={[
                { key: 'Deployments', name: 'Deployments', color: '#3b82f6' },
                { key: 'Failures', name: 'Failure-Linked', color: '#ef4444' },
              ]}
              height={220}
            />
          )}
        </ChartPanel>

        {/* Agent Performance */}
        <ChartPanel title="Agent Performance" loading={agentLoading}>
          {agentLoading ? (
            <ChartSkeleton height={220} />
          ) : !agentExec ? (
            <EmptyState icon={Zap} message="No agent execution data" />
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-zinc-800/40 rounded-lg p-3">
                  <p className="text-xs text-zinc-500 mb-1">Avg Steps / RCA</p>
                  <p className="text-xl font-semibold text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {Number(agentExec.avgStepsPerRca ?? 0).toFixed(1)}
                  </p>
                </div>
                <div className="bg-zinc-800/40 rounded-lg p-3">
                  <p className="text-xs text-zinc-500 mb-1">RCAs Completed</p>
                  <p className="text-xl font-semibold text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {agentExec.totalRcasCompleted}
                  </p>
                </div>
                <div className="bg-zinc-800/40 rounded-lg p-3">
                  <p className="text-xs text-zinc-500 mb-1">Tools Used</p>
                  <p className="text-xl font-semibold text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {agentExec.toolStats.length}
                  </p>
                </div>
              </div>
              {agentExec.toolStats.length > 0 && (
                <div className="overflow-hidden rounded-lg border border-zinc-800/60 max-h-48 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-zinc-900">
                      <tr className="border-b border-zinc-800/60 text-zinc-500 uppercase tracking-wider">
                        <th className="text-left px-3 py-2 font-medium">Tool</th>
                        <th className="text-right px-3 py-2 font-medium">Calls</th>
                        <th className="text-right px-3 py-2 font-medium">Incidents</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...agentExec.toolStats].sort((a, b) => b.totalCalls - a.totalCalls).slice(0, 10).map(t => (
                        <tr key={t.toolName} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors">
                          <td className="px-3 py-1.5 text-zinc-300 font-mono">{t.toolName}</td>
                          <td className="px-3 py-1.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.totalCalls}</td>
                          <td className="px-3 py-1.5 text-right text-zinc-400" style={{ fontVariantNumeric: 'tabular-nums' }}>{t.incidentsUsed}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </ChartPanel>
      </div>
    </div>
  );
}
