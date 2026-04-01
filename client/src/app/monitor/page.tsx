'use client';

import { useState, useEffect, useCallback } from 'react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Activity, Server, Clock, CheckCircle2, RefreshCw, ChevronRight,
  Wrench, Brain, Sparkles, AlertTriangle, DollarSign, Zap, Hash,
  Cpu, Gauge, RotateCcw, Layers, Timer,
} from 'lucide-react';

/* ================================================================
   Fleet Tab
   ================================================================ */
function FleetTab({ onViewTimeline }: { onViewTimeline: (id: string) => void }) {
  const [fleet, setFleet] = useState<Record<string, unknown>[] | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchFleet = useCallback(async () => {
    setLoading(true);
    try {
      const [fleetRes, summaryRes] = await Promise.all([
        fetch('/api/monitor/fleet'),
        fetch('/api/monitor/fleet/summary'),
      ]);
      if (fleetRes.ok) setFleet(await fleetRes.json());
      if (summaryRes.ok) setSummary(await summaryRes.json());
    } catch { /* */ } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchFleet(); }, [fetchFleet]);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard label="Total Runs" value={summary?.total_agent_runs} icon={<Server size={16} />} loading={loading} />
        <StatCard label="Active" value={summary?.active_count} icon={<Activity size={16} className="text-blue-500" />} loading={loading} />
        <StatCard label="Completed" value={summary?.completed_count} icon={<CheckCircle2 size={16} className="text-green-500" />} loading={loading} />
        <StatCard label="Errors" value={summary?.error_count} icon={<AlertTriangle size={16} className="text-destructive" />} loading={loading} />
        <StatCard
          label="Avg Duration"
          value={summary?.avg_rca_duration_seconds != null ? formatDuration(Number(summary.avg_rca_duration_seconds)) : '—'}
          icon={<Clock size={16} className="text-muted-foreground" />}
          loading={loading}
        />
      </div>

      <Card>
        <CardHeader className="pb-3 flex flex-row items-center justify-between">
          <CardTitle className="text-base">Agent Runs</CardTitle>
          <Button variant="ghost" size="sm" onClick={fetchFleet}><RefreshCw size={14} className="mr-1.5" /> Refresh</Button>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="p-6 space-y-3">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          ) : !fleet || fleet.length === 0 ? (
            <div className="p-8 text-center text-muted-foreground text-sm">No agent runs found</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40">
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Service</th>
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Environment</th>
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Status</th>
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Severity</th>
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Source</th>
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Started</th>
                    <th className="w-24" />
                  </tr>
                </thead>
                <tbody>
                  {fleet.map((row, i) => (
                    <tr key={i} className="border-b hover:bg-muted/30 transition-colors">
                      <td className="px-4 py-2.5 font-medium">{String(row.alert_service ?? '—')}</td>
                      <td className="px-4 py-2.5 text-muted-foreground">{String(row.alert_environment ?? '—')}</td>
                      <td className="px-4 py-2.5"><StatusBadge status={String(row.aurora_status ?? '')} /></td>
                      <td className="px-4 py-2.5"><SeverityBadge severity={String(row.severity ?? '')} /></td>
                      <td className="px-4 py-2.5 text-muted-foreground">{String(row.source_type ?? '—')}</td>
                      <td className="px-4 py-2.5 text-muted-foreground text-xs">
                        {row.started_at ? new Date(String(row.started_at)).toLocaleString() : '—'}
                      </td>
                      <td className="px-2">
                        <Button variant="ghost" size="sm" className="h-7 text-xs"
                          onClick={() => onViewTimeline(String(row.incident_id))}>
                          Timeline <ChevronRight size={12} className="ml-0.5" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* ================================================================
   Execution Waterfall Tab
   ================================================================ */
function WaterfallTab({ preselectedIncidentId }: { preselectedIncidentId?: string | null }) {
  const [incidents, setIncidents] = useState<Record<string, unknown>[]>([]);
  const [selectedId, setSelectedId] = useState<string>(preselectedIncidentId || '');
  const [timeline, setTimeline] = useState<{ summary: Record<string, unknown>; events: Record<string, unknown>[]; agent_session?: AgentSession | null } | null>(null);
  const [toolStats, setToolStats] = useState<{ tools: Record<string, unknown>[]; rca_summary: Record<string, unknown> } | null>(null);
  const [loading, setLoading] = useState(false);
  const [statsLoading, setStatsLoading] = useState(true);
  const [incidentsLoading, setIncidentsLoading] = useState(true);

  useEffect(() => {
    fetch('/api/monitor/fleet')
      .then((r) => r.ok ? r.json() : [])
      .then((d) => setIncidents(Array.isArray(d) ? d : []))
      .catch(() => {})
      .finally(() => setIncidentsLoading(false));
    fetch('/api/monitor/tools/stats')
      .then((r) => r.ok ? r.json() : null)
      .then((d) => { if (d) setToolStats(d); })
      .catch(() => {})
      .finally(() => setStatsLoading(false));
  }, []);

  const loadTimeline = useCallback(async (id: string) => {
    if (!id) return;
    setSelectedId(id);
    setLoading(true);
    setTimeline(null);
    try {
      const res = await fetch(`/api/monitor/incidents/${id}/timeline`);
      if (res.ok) setTimeline(await res.json());
    } catch { /* */ } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (preselectedIncidentId) loadTimeline(preselectedIncidentId);
  }, [preselectedIncidentId, loadTimeline]);

  const rcaStats = toolStats?.rca_summary;

  return (
    <div className="space-y-6">
      {/* RCA aggregate stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total RCAs" value={rcaStats?.total_rcas} icon={<Zap size={16} />} loading={statsLoading} />
        <StatCard label="Avg Tools/RCA" value={rcaStats?.avg_tool_calls_per_rca} icon={<Wrench size={16} className="text-blue-500" />} loading={statsLoading} />
        <StatCard label="Avg Duration" value={rcaStats?.avg_rca_duration_seconds != null ? formatDuration(Number(rcaStats.avg_rca_duration_seconds)) : '—'} icon={<Clock size={16} className="text-muted-foreground" />} loading={statsLoading} />
        <StatCard label="Avg Cost/RCA" value={rcaStats?.avg_cost_per_rca != null ? `$${Number(rcaStats.avg_cost_per_rca).toFixed(3)}` : '—'} icon={<DollarSign size={16} className="text-green-500" />} loading={statsLoading} />
      </div>

      {/* Tool breakdown */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2"><Wrench size={16} /> Tool Usage (last 30d)</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {statsLoading ? (
            <div className="p-6 space-y-3">{[...Array(3)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
          ) : !toolStats?.tools?.length ? (
            <div className="p-8 text-center text-muted-foreground text-sm">No tool usage data</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40">
                    <th className="text-left px-4 py-2.5 font-medium text-muted-foreground">Tool</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">Calls</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">Incidents</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">Avg (ms)</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">P95 (ms)</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">Errors</th>
                    <th className="text-right px-4 py-2.5 font-medium text-muted-foreground">Success %</th>
                  </tr>
                </thead>
                <tbody>
                  {toolStats.tools.map((t, i) => (
                    <tr key={i} className="border-b hover:bg-muted/30 transition-colors">
                      <td className="px-4 py-2.5 font-medium font-mono text-xs">{String(t.tool_name)}</td>
                      <td className="px-4 py-2.5 text-right">{String(t.call_count)}</td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground">{String(t.incident_count)}</td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground">{t.avg_duration_ms != null ? Number(t.avg_duration_ms).toLocaleString() : '—'}</td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground">{t.p95_duration_ms != null ? Number(t.p95_duration_ms).toLocaleString() : '—'}</td>
                      <td className={`px-4 py-2.5 text-right ${Number(t.error_count ?? 0) > 0 ? 'text-destructive font-medium' : 'text-muted-foreground'}`}>{String(t.error_count ?? 0)}</td>
                      <td className="px-4 py-2.5 text-right">{t.success_rate != null ? `${Number(t.success_rate)}%` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Incident picker */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Select Incident for Timeline</CardTitle>
        </CardHeader>
        <CardContent>
          {incidentsLoading ? (
            <Skeleton className="h-10 w-full" />
          ) : incidents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No incidents with agent runs found</p>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
              {incidents.slice(0, 12).map((inc, i) => {
                const id = String(inc.incident_id);
                const isSelected = selectedId === id;
                return (
                  <button key={i} onClick={() => loadTimeline(id)}
                    className={`text-left p-3 rounded-md border text-sm transition-colors ${isSelected ? 'border-primary bg-primary/5 shadow-sm' : 'border-border/50 hover:border-border hover:bg-muted/30'}`}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-medium truncate">{String(inc.alert_service ?? 'Unknown')}</span>
                      <StatusBadge status={String(inc.aurora_status ?? '')} />
                    </div>
                    <div className="text-xs text-muted-foreground truncate">
                      {String(inc.alert_environment ?? '')} &middot; {String(inc.source_type ?? '')}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Timeline */}
      {loading && (
        <Card><CardContent className="pt-6"><div className="space-y-3">{[...Array(6)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div></CardContent></Card>
      )}
      {timeline && <TimelineView timeline={timeline} />}
    </div>
  );
}

/* ================================================================
   Timeline View
   ================================================================ */
function TimelineView({ timeline }: { timeline: { summary: Record<string, unknown>; events: Record<string, unknown>[]; agent_session?: AgentSession | null } }) {
  const { summary, events, agent_session } = timeline;

  const toolCalls = events.filter(e => e.event_type === 'tool_call');
  const llmCalls = events.filter(e => e.event_type === 'llm_call');
  const maxDuration = Math.max(...toolCalls.map(e => Number(e.response_time_ms ?? 0)), 1);

  return (
    <div className="space-y-4">
      {/* Summary bar */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
            <span className="flex items-center gap-1.5">
              <StatusBadge status={String(summary.aurora_status ?? '')} />
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Wrench size={14} /> <strong className="text-foreground">{String(summary.total_tool_calls ?? 0)}</strong> tool calls
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Brain size={14} /> <strong className="text-foreground">{String(summary.total_thoughts ?? 0)}</strong> thoughts
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Sparkles size={14} /> <strong className="text-foreground">{String(summary.total_llm_calls ?? 0)}</strong> LLM calls
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Hash size={14} /> <strong className="text-foreground">{summary.total_tokens != null ? Number(summary.total_tokens).toLocaleString() : '—'}</strong> tokens
            </span>
            {summary.total_cost != null && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <DollarSign size={14} /> <strong className="text-foreground">${Number(summary.total_cost).toFixed(3)}</strong>
              </span>
            )}
            {summary.duration_seconds != null && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Clock size={14} /> <strong className="text-foreground">{formatDuration(Number(summary.duration_seconds))}</strong>
              </span>
            )}
            {Number(summary.tool_errors ?? 0) > 0 && (
              <span className="flex items-center gap-1.5 text-destructive">
                <AlertTriangle size={14} /> <strong>{String(summary.tool_errors)}</strong> tool errors
              </span>
            )}
            {summary.avg_tool_duration_ms != null && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Zap size={14} /> avg tool <strong className="text-foreground">{Number(summary.avg_tool_duration_ms).toLocaleString()}ms</strong>
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Agent session telemetry */}
      {agent_session && <AgentSessionDetail session={agent_session as AgentSession} />}

      {/* Event timeline */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Execution Timeline ({events.length} events)</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y">
            {events.map((ev, i) => (
              <TimelineEvent key={i} event={ev} index={i} maxDuration={maxDuration} />
            ))}
          </div>
        </CardContent>
      </Card>

      {/* LLM call breakdown */}
      {llmCalls.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2"><Sparkles size={16} /> LLM Calls ({llmCalls.length})</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40">
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Model</th>
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Type</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Tokens</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Response (ms)</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Cost</th>
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {llmCalls.map((ev, i) => (
                    <tr key={i} className={`border-b hover:bg-muted/30 ${ev.output ? 'bg-destructive/5' : ''}`}>
                      <td className="px-4 py-2 font-mono text-xs">{String(ev.label ?? '—')}</td>
                      <td className="px-4 py-2 text-muted-foreground text-xs">{String(ev.detail ?? '—')}</td>
                      <td className="px-4 py-2 text-right">{ev.tokens != null ? Number(ev.tokens).toLocaleString() : '—'}</td>
                      <td className="px-4 py-2 text-right text-muted-foreground">{ev.response_time_ms != null ? Number(ev.response_time_ms).toLocaleString() : '—'}</td>
                      <td className="px-4 py-2 text-right">{ev.cost != null ? `$${Number(ev.cost).toFixed(4)}` : '—'}</td>
                      <td className="px-4 py-2 text-muted-foreground text-xs">{ev.event_time ? new Date(String(ev.event_time)).toLocaleTimeString() : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function TimelineEvent({ event: ev, index, maxDuration }: { event: Record<string, unknown>; index: number; maxDuration: number }) {
  const [expanded, setExpanded] = useState(false);
  const eventType = String(ev.event_type);
  const isToolCall = eventType === 'tool_call';
  const isThought = eventType === 'thought';
  const isLlm = eventType === 'llm_call';
  const isError = String(ev.step_status ?? '') === 'error';
  const durationMs = ev.response_time_ms != null ? Number(ev.response_time_ms) : null;

  const icon = isToolCall ? <Wrench size={14} /> : isThought ? <Brain size={14} /> : <Sparkles size={14} />;
  const color = isError ? 'text-destructive' : isToolCall ? 'text-blue-500' : isThought ? 'text-purple-500' : 'text-amber-500';
  const bgColor = isError ? 'bg-destructive' : isToolCall ? 'bg-blue-500' : isThought ? 'bg-purple-500' : 'bg-amber-500';

  const hasDetail = (isToolCall && (ev.detail || ev.output)) || (isThought && ev.detail);

  return (
    <div className={`relative ${isError ? 'bg-destructive/5' : ''}`}>
      <button
        onClick={() => hasDetail && setExpanded(!expanded)}
        className={`w-full text-left px-4 py-3 flex items-start gap-3 transition-colors ${hasDetail ? 'hover:bg-muted/30 cursor-pointer' : ''}`}
      >
        <div className="flex flex-col items-center pt-0.5 shrink-0">
          <div className={`w-2.5 h-2.5 rounded-full ${bgColor} ring-2 ring-background`} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={color}>{icon}</span>
            <span className="font-medium text-sm">{String(ev.label ?? '—')}</span>
            <EventBadge type={eventType} />
            {isError && (
              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-destructive/10 text-destructive">
                <AlertTriangle size={10} className="mr-0.5" /> error
              </span>
            )}
            {isToolCall && durationMs != null && (
              <span className="text-xs text-muted-foreground">{durationMs.toLocaleString()}ms</span>
            )}
            {isLlm && ev.tokens != null && (
              <span className="text-xs text-muted-foreground">{Number(ev.tokens).toLocaleString()} tokens</span>
            )}
            {isLlm && durationMs != null && (
              <span className="text-xs text-muted-foreground">{durationMs.toLocaleString()}ms</span>
            )}
          </div>
          {isToolCall && ev.detail && (
            <p className="text-xs text-muted-foreground mt-0.5 truncate">{String(ev.detail)}</p>
          )}
          {/* Duration bar for tool calls */}
          {isToolCall && durationMs != null && maxDuration > 0 && (
            <div className="mt-1.5 flex items-center gap-2">
              <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden max-w-[200px]">
                <div
                  className={`h-full rounded-full ${isError ? 'bg-destructive' : 'bg-blue-500'}`}
                  style={{ width: `${Math.max(2, (durationMs / maxDuration) * 100)}%` }}
                />
              </div>
            </div>
          )}
        </div>

        <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">
          {ev.event_time ? new Date(String(ev.event_time)).toLocaleTimeString() : ''}
        </span>
      </button>

      {expanded && hasDetail && (
        <div className="px-4 pb-3 ml-[26px]">
          {isError && ev.step_error && (
            <div className="bg-destructive/10 border border-destructive/20 rounded p-2 text-xs text-destructive mb-2">
              <strong>Error:</strong> {String(ev.step_error)}
            </div>
          )}
          {isThought && ev.detail && (
            <div className="bg-muted/50 rounded p-3 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
              {String(ev.detail)}
            </div>
          )}
          {isToolCall && ev.detail && (
            <div className="space-y-2">
              <div>
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">Command</span>
                <pre className="bg-muted/50 rounded p-2 text-xs mt-0.5 overflow-x-auto">{String(ev.detail)}</pre>
              </div>
              {ev.output && (
                <div>
                  <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">Output</span>
                  <pre className="bg-muted/50 rounded p-2 text-xs mt-0.5 max-h-40 overflow-y-auto overflow-x-auto">{String(ev.output)}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ================================================================
   Shared components
   ================================================================ */
function StatCard({ label, value, icon, loading }: { label: string; value: unknown; icon: React.ReactNode; loading?: boolean }) {
  return (
    <Card>
      <CardContent className="pt-4 pb-3">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] text-muted-foreground uppercase tracking-wider">{label}</span>
          {icon}
        </div>
        {loading ? <Skeleton className="h-7 w-16" /> : <p className="text-xl font-semibold">{String(value ?? '—')}</p>}
      </CardContent>
    </Card>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { variant: 'default' | 'secondary' | 'destructive' | 'outline'; label: string }> = {
    analyzing: { variant: 'default', label: 'Analyzing' },
    running: { variant: 'default', label: 'Running' },
    pending: { variant: 'default', label: 'Pending' },
    complete: { variant: 'secondary', label: 'Complete' },
    completed: { variant: 'secondary', label: 'Completed' },
    resolved: { variant: 'secondary', label: 'Resolved' },
    analyzed: { variant: 'secondary', label: 'Analyzed' },
    success: { variant: 'secondary', label: 'Success' },
    error: { variant: 'destructive', label: 'Error' },
    idle: { variant: 'outline', label: 'Idle' },
  };
  const cfg = map[status] || { variant: 'outline' as const, label: status || '—' };
  return <Badge variant={cfg.variant} className="text-xs">{cfg.label}</Badge>;
}

function SeverityBadge({ severity }: { severity: string }) {
  const colors: Record<string, string> = {
    critical: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
    high: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
    medium: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
    low: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  };
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${colors[severity] || 'bg-muted text-muted-foreground'}`}>
      {severity || '—'}
    </span>
  );
}

function EventBadge({ type }: { type: string }) {
  const styles: Record<string, string> = {
    tool_call: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
    thought: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
    llm_call: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  };
  const labels: Record<string, string> = { tool_call: 'tool', thought: 'thought', llm_call: 'llm' };
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${styles[type] || 'bg-muted text-muted-foreground'}`}>
      {labels[type] || type}
    </span>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

/* ================================================================
   Agent Sessions Tab — full telemetry for every agent run
   ================================================================ */
interface AgentSession {
  id: number;
  session_id: string;
  incident_id: string | null;
  model_name: string | null;
  detected_provider: string | null;
  provider_mode: string | null;
  use_direct_sdk: boolean | null;
  temperature: number | null;
  mode: string | null;
  is_background: boolean;
  recursion_limit: number | null;
  context_messages_loaded: number | null;
  context_load_ms: number | null;
  rca_compression_applied: boolean;
  rca_compression_before: number | null;
  rca_compression_after: number | null;
  preflight_compression_applied: boolean;
  middleware_trim_applied: boolean;
  middleware_tokens_before: number | null;
  middleware_tokens_after: number | null;
  time_to_first_token_ms: number | null;
  total_events: number | null;
  total_tokens_streamed: number | null;
  model_turns: number | null;
  tool_calls_count: number | null;
  tool_errors_count: number | null;
  retry_attempts: number | null;
  last_retry_error: string | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_llm_calls: number | null;
  total_cost: number | null;
  status: string;
  error_message: string | null;
  placeholder_warning: boolean;
  duration_ms: number | null;
  started_at: string | null;
  completed_at: string | null;
  alert_service: string | null;
  alert_title: string | null;
  severity: string | null;
}

function AgentSessionsTab() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<number | null>(null);

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/monitor/agent-sessions');
      if (res.ok) {
        const data = await res.json();
        setSessions(data.sessions ?? []);
        setSummary(data.summary ?? null);
      }
    } catch { /* */ } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchSessions(); }, [fetchSessions]);

  return (
    <div className="space-y-6">
      {/* Aggregate stats */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard label="Total Sessions" value={summary?.total_sessions} icon={<Layers size={16} />} loading={loading} />
        <StatCard label="Avg TTFT" value={summary?.avg_ttft_ms != null ? `${Number(summary.avg_ttft_ms).toLocaleString()}ms` : '—'} icon={<Timer size={16} className="text-blue-500" />} loading={loading} />
        <StatCard label="Avg Duration" value={summary?.avg_duration_ms != null ? formatDuration(Number(summary.avg_duration_ms) / 1000) : '—'} icon={<Clock size={16} className="text-muted-foreground" />} loading={loading} />
        <StatCard label="Avg Cost" value={summary?.avg_cost != null ? `$${Number(summary.avg_cost).toFixed(3)}` : '—'} icon={<DollarSign size={16} className="text-green-500" />} loading={loading} />
        <StatCard label="Total Retries" value={summary?.total_retries ?? 0} icon={<RotateCcw size={16} className="text-orange-500" />} loading={loading} />
      </div>

      <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
        <StatCard label="Completed" value={summary?.completed} icon={<CheckCircle2 size={16} className="text-green-500" />} loading={loading} />
        <StatCard label="Errors" value={summary?.errored} icon={<AlertTriangle size={16} className="text-destructive" />} loading={loading} />
        <StatCard label="Avg Model Turns" value={summary?.avg_model_turns} icon={<Cpu size={16} className="text-purple-500" />} loading={loading} />
        <StatCard label="Avg Tool Calls" value={summary?.avg_tool_calls} icon={<Wrench size={16} className="text-blue-500" />} loading={loading} />
        <StatCard label="Context Compressions" value={Number(summary?.rca_compressions ?? 0) + Number(summary?.preflight_compressions ?? 0)} icon={<Gauge size={16} className="text-amber-500" />} loading={loading} />
        <StatCard label="Middleware Trims" value={summary?.middleware_trims} icon={<AlertTriangle size={16} className="text-orange-500" />} loading={loading} />
      </div>

      {/* Sessions list */}
      <Card>
        <CardHeader className="pb-3 flex flex-row items-center justify-between">
          <CardTitle className="text-base">Agent Sessions</CardTitle>
          <Button variant="ghost" size="sm" onClick={fetchSessions}><RefreshCw size={14} className="mr-1.5" /> Refresh</Button>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="p-6 space-y-3">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          ) : sessions.length === 0 ? (
            <div className="p-8 text-center text-muted-foreground text-sm">No agent sessions recorded yet. Sessions will appear after the next agent run.</div>
          ) : (
            <div className="divide-y">
              {sessions.map((s) => (
                <div key={s.id}>
                  <button
                    onClick={() => setExpanded(expanded === s.id ? null : s.id)}
                    className="w-full text-left px-4 py-3 hover:bg-muted/30 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-sm">{s.alert_service ?? s.session_id.slice(0, 8)}</span>
                          <StatusBadge status={s.status} />
                          {s.is_background && <Badge variant="outline" className="text-[10px]">background</Badge>}
                          {s.model_name && <span className="text-xs font-mono text-muted-foreground">{s.model_name}</span>}
                        </div>
                        <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground flex-wrap">
                          {s.duration_ms != null && <span><Clock size={10} className="inline mr-0.5" />{formatDuration(s.duration_ms / 1000)}</span>}
                          {s.time_to_first_token_ms != null && <span>TTFT: {s.time_to_first_token_ms.toLocaleString()}ms</span>}
                          {s.model_turns != null && <span>{s.model_turns} turns</span>}
                          {s.tool_calls_count != null && <span>{s.tool_calls_count} tools</span>}
                          {s.total_cost != null && <span>${Number(s.total_cost).toFixed(3)}</span>}
                          {(s.retry_attempts ?? 0) > 0 && <span className="text-orange-500">{s.retry_attempts} retries</span>}
                          {(s.tool_errors_count ?? 0) > 0 && <span className="text-destructive">{s.tool_errors_count} errors</span>}
                        </div>
                      </div>
                      <span className="text-xs text-muted-foreground shrink-0">
                        {s.started_at ? new Date(s.started_at).toLocaleString() : ''}
                      </span>
                      <ChevronRight size={14} className={`text-muted-foreground transition-transform ${expanded === s.id ? 'rotate-90' : ''}`} />
                    </div>
                  </button>

                  {expanded === s.id && (
                    <div className="px-4 pb-4">
                      <AgentSessionDetail session={s} />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function AgentSessionDetail({ session: s }: { session: AgentSession }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
      {/* Model & Routing */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Cpu size={12} /> Model & Routing</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Model" value={s.model_name} />
          <DetailRow label="Provider" value={s.detected_provider} />
          <DetailRow label="Mode" value={s.provider_mode} />
          <DetailRow label="Direct SDK" value={s.use_direct_sdk != null ? String(s.use_direct_sdk) : null} />
          <DetailRow label="Temperature" value={s.temperature != null ? String(s.temperature) : null} />
          <DetailRow label="Chat Mode" value={s.mode} />
          <DetailRow label="Recursion Limit" value={s.recursion_limit != null ? String(s.recursion_limit) : null} />
        </CardContent>
      </Card>

      {/* Context & Compression */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Gauge size={12} /> Context & Compression</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Messages Loaded" value={s.context_messages_loaded != null ? String(s.context_messages_loaded) : null} />
          <DetailRow label="Context Load" value={s.context_load_ms != null ? `${s.context_load_ms}ms` : null} />
          <DetailRow label="RCA Compression" value={s.rca_compression_applied ? `${s.rca_compression_before} → ${s.rca_compression_after} msgs` : 'No'} />
          <DetailRow label="Preflight Compress" value={s.preflight_compression_applied ? 'Yes' : 'No'} />
          <DetailRow label="Middleware Trim" value={s.middleware_trim_applied ? `${s.middleware_tokens_before} → ${s.middleware_tokens_after} tokens` : 'No'} />
        </CardContent>
      </Card>

      {/* Execution Stats */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Zap size={12} /> Execution</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Duration" value={s.duration_ms != null ? formatDuration(s.duration_ms / 1000) : null} />
          <DetailRow label="Time to First Token" value={s.time_to_first_token_ms != null ? `${s.time_to_first_token_ms.toLocaleString()}ms` : null} />
          <DetailRow label="Model Turns" value={s.model_turns != null ? String(s.model_turns) : null} />
          <DetailRow label="Tool Calls" value={s.tool_calls_count != null ? String(s.tool_calls_count) : null} />
          <DetailRow label="Tool Errors" value={s.tool_errors_count != null ? String(s.tool_errors_count) : null} highlight={!!s.tool_errors_count} />
          <DetailRow label="Total Events" value={s.total_events != null ? String(s.total_events) : null} />
          <DetailRow label="Tokens Streamed" value={s.total_tokens_streamed != null ? s.total_tokens_streamed.toLocaleString() : null} />
          <DetailRow label="Retries" value={s.retry_attempts != null ? String(s.retry_attempts) : null} highlight={!!s.retry_attempts} />
          {s.last_retry_error && <DetailRow label="Last Retry Error" value={s.last_retry_error} highlight />}
        </CardContent>
      </Card>

      {/* LLM Usage */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Sparkles size={12} /> LLM Usage</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="LLM Calls" value={s.total_llm_calls != null ? String(s.total_llm_calls) : null} />
          <DetailRow label="Input Tokens" value={s.total_input_tokens != null ? s.total_input_tokens.toLocaleString() : null} />
          <DetailRow label="Output Tokens" value={s.total_output_tokens != null ? s.total_output_tokens.toLocaleString() : null} />
          <DetailRow label="Total Cost" value={s.total_cost != null ? `$${Number(s.total_cost).toFixed(4)}` : null} />
        </CardContent>
      </Card>

      {/* Outcome */}
      <Card className="bg-muted/30 md:col-span-2">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><CheckCircle2 size={12} /> Outcome</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Status" value={s.status} />
          {s.error_message && <DetailRow label="Error" value={s.error_message} highlight />}
          <DetailRow label="Placeholder Warning" value={s.placeholder_warning ? 'Yes — AI output contained placeholder tokens' : 'No'} highlight={s.placeholder_warning} />
          <DetailRow label="Started" value={s.started_at ? new Date(s.started_at).toLocaleString() : null} />
          <DetailRow label="Completed" value={s.completed_at ? new Date(s.completed_at).toLocaleString() : null} />
          {s.alert_title && <DetailRow label="Alert" value={s.alert_title} />}
          {s.severity && <DetailRow label="Severity" value={s.severity} />}
        </CardContent>
      </Card>
    </div>
  );
}

function DetailRow({ label, value, highlight }: { label: string; value: string | null | undefined; highlight?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted-foreground shrink-0">{label}</span>
      <span className={`text-right truncate font-mono ${highlight ? 'text-destructive font-medium' : ''}`}>{value ?? '—'}</span>
    </div>
  );
}

/* ================================================================
   Page
   ================================================================ */
export default function MonitorPage() {
  const [activeTab, setActiveTab] = useState('fleet');
  const [timelineIncidentId, setTimelineIncidentId] = useState<string | null>(null);

  const openTimeline = (incidentId: string) => {
    setTimelineIncidentId(incidentId);
    setActiveTab('waterfall');
  };

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Monitor</h1>
        <p className="text-sm text-muted-foreground mt-1">Agent fleet and execution timelines</p>
      </div>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="fleet" className="gap-1.5"><Server size={14} /> Fleet</TabsTrigger>
          <TabsTrigger value="waterfall" className="gap-1.5"><Activity size={14} /> Execution Waterfall</TabsTrigger>
          <TabsTrigger value="sessions" className="gap-1.5"><Cpu size={14} /> Agent Sessions</TabsTrigger>
        </TabsList>
        <TabsContent value="fleet"><FleetTab onViewTimeline={openTimeline} /></TabsContent>
        <TabsContent value="waterfall"><WaterfallTab preselectedIncidentId={timelineIncidentId} /></TabsContent>
        <TabsContent value="sessions"><AgentSessionsTab /></TabsContent>
      </Tabs>
    </div>
  );
}
