'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Activity, Server, Clock, CheckCircle2, RefreshCw, ChevronRight,
  Wrench, Brain, Sparkles, AlertTriangle, DollarSign, Zap, Hash,
  Cpu, Gauge, RotateCcw, Layers, Timer, Radio,
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

interface TimelineData {
  summary: Record<string, unknown>;
  steps: Record<string, unknown>[];
  llm_calls: Record<string, unknown>[];
  thoughts: Record<string, unknown>[];
  agent_session?: AgentSession | null;
}

const RUNNING_STATUSES = new Set(['analyzing', 'running', 'pending']);

function WaterfallTab({ preselectedIncidentId }: { preselectedIncidentId?: string | null }) {
  const [incidents, setIncidents] = useState<Record<string, unknown>[]>([]);
  const [selectedId, setSelectedId] = useState<string>(preselectedIncidentId || '');
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [toolStats, setToolStats] = useState<{ tools: Record<string, unknown>[]; rca_summary: Record<string, unknown> } | null>(null);
  const [loading, setLoading] = useState(false);
  const [statsLoading, setStatsLoading] = useState(true);
  const [incidentsLoading, setIncidentsLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);

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

  useEffect(() => {
    return () => { eventSourceRef.current?.close(); };
  }, []);

  const connectSSE = useCallback((incidentId: string) => {
    eventSourceRef.current?.close();
    const es = new EventSource(`/api/monitor/incidents/${incidentId}/stream`);
    eventSourceRef.current = es;
    setStreaming(true);

    es.addEventListener('step', (e: MessageEvent) => {
      try {
        const step = JSON.parse(e.data);
        setTimeline(prev => {
          if (!prev) return prev;
          const idx = prev.steps.findIndex(s => s.id === step.id);
          const updated = [...prev.steps];
          if (idx >= 0) updated[idx] = step;
          else updated.push(step);
          return { ...prev, steps: updated };
        });
      } catch { /* */ }
    });

    es.addEventListener('thought', (e: MessageEvent) => {
      try {
        const thought = JSON.parse(e.data);
        setTimeline(prev => {
          if (!prev) return prev;
          if (prev.thoughts.some(t => t.id === thought.id)) return prev;
          return { ...prev, thoughts: [...prev.thoughts, thought] };
        });
      } catch { /* */ }
    });

    es.addEventListener('llm_call', (e: MessageEvent) => {
      try {
        const llm = JSON.parse(e.data);
        setTimeline(prev => {
          if (!prev) return prev;
          if (prev.llm_calls.some(l => l.id === llm.id)) return prev;
          return { ...prev, llm_calls: [...prev.llm_calls, llm] };
        });
      } catch { /* */ }
    });

    es.addEventListener('session', (e: MessageEvent) => {
      try {
        const session = JSON.parse(e.data);
        setTimeline(prev => {
          if (!prev) return prev;
          return { ...prev, agent_session: { ...prev.agent_session, ...session } as AgentSession };
        });
      } catch { /* */ }
    });

    es.addEventListener('done', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        setTimeline(prev => prev ? { ...prev, summary: { ...prev.summary, aurora_status: data.status } } : prev);
      } catch { /* */ }
      es.close();
      eventSourceRef.current = null;
      setStreaming(false);
    });

    es.addEventListener('error', () => {
      if (es.readyState === EventSource.CLOSED) {
        setStreaming(false);
        eventSourceRef.current = null;
      }
    });
  }, []);

  const loadTimeline = useCallback(async (id: string) => {
    if (!id) return;
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    setStreaming(false);
    setSelectedId(id);
    setLoading(true);
    setTimeline(null);
    try {
      const res = await fetch(`/api/monitor/incidents/${id}/timeline`);
      if (res.ok) {
        const data: TimelineData = await res.json();
        setTimeline(data);
        if (RUNNING_STATUSES.has(String(data.summary?.aurora_status ?? ''))) {
          connectSSE(id);
        }
      }
    } catch { /* */ } finally { setLoading(false); }
  }, [connectSSE]);

  useEffect(() => {
    if (preselectedIncidentId) loadTimeline(preselectedIncidentId);
  }, [preselectedIncidentId, loadTimeline]);

  const rcaStats = toolStats?.rca_summary;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total RCAs" value={rcaStats?.total_rcas} icon={<Zap size={16} />} loading={statsLoading} />
        <StatCard label="Avg Tools/RCA" value={rcaStats?.avg_tool_calls_per_rca} icon={<Wrench size={16} className="text-blue-500" />} loading={statsLoading} />
        <StatCard label="Avg Duration" value={rcaStats?.avg_rca_duration_seconds != null ? formatDuration(Number(rcaStats.avg_rca_duration_seconds)) : '—'} icon={<Clock size={16} className="text-muted-foreground" />} loading={statsLoading} />
        <StatCard label="Avg Cost/RCA" value={rcaStats?.avg_cost_per_rca != null ? `$${Number(rcaStats.avg_cost_per_rca).toFixed(3)}` : '—'} icon={<DollarSign size={16} className="text-green-500" />} loading={statsLoading} />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2"><Wrench size={16} /> Tool Performance (last 30d)</CardTitle>
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

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Select Incident</CardTitle>
        </CardHeader>
        <CardContent>
          {incidentsLoading ? (
            <Skeleton className="h-10 w-full" />
          ) : incidents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No incidents with agent runs found</p>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
              {incidents.map((inc, i) => {
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

      {loading && (
        <Card><CardContent className="pt-6"><div className="space-y-3">{[...Array(6)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div></CardContent></Card>
      )}
      {timeline && <TimelineView data={timeline} streaming={streaming} />}
    </div>
  );
}

/* ================================================================
   Timeline View — clean separation: steps, LLM calls, thoughts
   ================================================================ */
function TimelineView({ data, streaming }: { data: TimelineData; streaming?: boolean }) {
  const { summary, steps, llm_calls, thoughts, agent_session } = data;
  const endRef = useRef<HTMLDivElement>(null);
  const [showThoughts, setShowThoughts] = useState(false);

  useEffect(() => {
    if (streaming && endRef.current) endRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [streaming, steps.length]);

  const maxDuration = Math.max(...steps.map(s => Number(s.duration_ms ?? 0)), 1);
  const errorSteps = steps.filter(s => s.status === 'error');

  return (
    <div className="space-y-4">
      {/* Summary bar */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
            <span className="flex items-center gap-1.5">
              <StatusBadge status={String(summary.aurora_status ?? '')} />
              {streaming && (
                <span className="inline-flex items-center gap-1 ml-1 text-xs text-green-500 font-medium">
                  <Radio size={12} className="animate-pulse" /> Live
                </span>
              )}
            </span>
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Wrench size={14} /> <strong className="text-foreground">{steps.length}</strong> tool calls
            </span>
            {errorSteps.length > 0 && (
              <span className="flex items-center gap-1.5 text-destructive">
                <AlertTriangle size={14} /> <strong>{errorSteps.length}</strong> errors
              </span>
            )}
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Sparkles size={14} /> <strong className="text-foreground">{llm_calls.length}</strong> LLM calls
            </span>
            {summary.total_tokens != null && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Hash size={14} /> <strong className="text-foreground">{Number(summary.total_tokens).toLocaleString()}</strong> tokens
              </span>
            )}
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
            {summary.avg_tool_duration_ms != null && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Zap size={14} /> avg <strong className="text-foreground">{Number(summary.avg_tool_duration_ms).toLocaleString()}ms</strong>
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Tool call waterfall */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base flex items-center gap-2">
              <Wrench size={16} /> Tool Calls ({steps.length})
            </CardTitle>
            {streaming && (
              <div className="flex items-center gap-1.5 text-xs text-green-500 animate-pulse">
                <Radio size={14} /> <span className="font-medium">Live</span>
              </div>
            )}
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y">
            {steps.map((step) => (
              <StepRow key={step.id as number} step={step} maxDuration={maxDuration} />
            ))}
            {streaming && (
              <div className="px-4 py-3 flex items-center gap-2 text-xs text-muted-foreground bg-muted/20">
                <div className="w-2.5 h-2.5 rounded-full bg-green-500 animate-pulse ring-2 ring-background" />
                <span>Waiting for next tool call...</span>
              </div>
            )}
          </div>
          <div ref={endRef} />
        </CardContent>
      </Card>

      {/* LLM calls table */}
      {llm_calls.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2"><Sparkles size={16} /> LLM Calls ({llm_calls.length})</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40">
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Model</th>
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Type</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Tokens</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Latency</th>
                    <th className="text-right px-4 py-2 font-medium text-muted-foreground">Cost</th>
                    <th className="text-left px-4 py-2 font-medium text-muted-foreground">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {llm_calls.map((lc) => (
                    <tr key={lc.id as number} className={`border-b hover:bg-muted/30 ${lc.error_message ? 'bg-destructive/5' : ''}`}>
                      <td className="px-4 py-2 font-mono text-xs">{String(lc.model_name ?? '—')}</td>
                      <td className="px-4 py-2 text-muted-foreground text-xs">{String(lc.request_type ?? '—')}</td>
                      <td className="px-4 py-2 text-right">{lc.total_tokens != null ? Number(lc.total_tokens).toLocaleString() : '—'}</td>
                      <td className="px-4 py-2 text-right text-muted-foreground">{lc.response_time_ms != null ? `${Number(lc.response_time_ms).toLocaleString()}ms` : '—'}</td>
                      <td className="px-4 py-2 text-right">{lc.cost != null ? `$${Number(lc.cost).toFixed(4)}` : '—'}</td>
                      <td className="px-4 py-2 text-muted-foreground text-xs">{lc.timestamp ? new Date(String(lc.timestamp)).toLocaleTimeString() : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Thoughts — collapsed by default */}
      {thoughts.length > 0 && (
        <Card>
          <CardHeader className="pb-0">
            <button onClick={() => setShowThoughts(!showThoughts)} className="flex items-center gap-2 text-sm hover:text-foreground text-muted-foreground transition-colors">
              <Brain size={16} />
              <span className="font-medium">{thoughts.length} Agent Thoughts</span>
              <ChevronRight size={14} className={`transition-transform ${showThoughts ? 'rotate-90' : ''}`} />
            </button>
          </CardHeader>
          {showThoughts && (
            <CardContent className="pt-3">
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {thoughts.map((t) => (
                  <div key={t.id as number} className="text-xs border-l-2 border-purple-500/30 pl-3 py-1">
                    <span className="text-muted-foreground">{t.event_time ? new Date(String(t.event_time)).toLocaleTimeString() : ''}</span>
                    <span className="ml-2 text-purple-500 font-medium">{String(t.thought_type ?? '')}</span>
                    <p className="mt-0.5 text-muted-foreground">{String(t.content ?? '')}</p>
                  </div>
                ))}
              </div>
            </CardContent>
          )}
        </Card>
      )}

      {/* Agent session telemetry — collapsible */}
      {agent_session && <AgentSessionCollapsible session={agent_session as AgentSession} />}
    </div>
  );
}

function StepRow({ step, maxDuration }: { step: Record<string, unknown>; maxDuration: number }) {
  const [expanded, setExpanded] = useState(false);
  const isError = step.status === 'error';
  const isRunning = step.status === 'running';
  const durationMs = step.duration_ms != null ? Number(step.duration_ms) : null;
  const inputStr = step.tool_input ? (typeof step.tool_input === 'string' ? step.tool_input : JSON.stringify(step.tool_input, null, 2)) : null;
  const outputStr = step.tool_output ? String(step.tool_output) : null;

  return (
    <div className={isError ? 'bg-destructive/5' : ''}>
      <button onClick={() => setExpanded(!expanded)}
        className="w-full text-left px-4 py-2.5 flex items-center gap-3 hover:bg-muted/30 transition-colors">
        <div className={`w-2 h-2 rounded-full shrink-0 ${isError ? 'bg-destructive' : isRunning ? 'bg-amber-500 animate-pulse' : 'bg-blue-500'}`} />
        <span className="font-mono text-xs font-medium min-w-0 truncate">{String(step.tool_name ?? '—')}</span>
        {isError && (
          <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-destructive/10 text-destructive shrink-0">
            <AlertTriangle size={10} className="mr-0.5" /> error
          </span>
        )}
        {isRunning && (
          <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 animate-pulse shrink-0">
            <RefreshCw size={10} className="mr-0.5 animate-spin" /> running
          </span>
        )}
        {durationMs != null && (
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden max-w-[180px]">
              <div className={`h-full rounded-full ${isError ? 'bg-destructive' : 'bg-blue-500'}`}
                style={{ width: `${Math.max(3, (durationMs / maxDuration) * 100)}%` }} />
            </div>
            <span className="text-xs text-muted-foreground shrink-0">{durationMs.toLocaleString()}ms</span>
          </div>
        )}
        <span className="text-xs text-muted-foreground shrink-0 ml-auto">
          {step.started_at ? new Date(String(step.started_at)).toLocaleTimeString() : ''}
        </span>
        <ChevronRight size={12} className={`text-muted-foreground transition-transform shrink-0 ${expanded ? 'rotate-90' : ''}`} />
      </button>
      {expanded && (
        <div className="px-4 pb-3 ml-5 space-y-2">
          {isError && step.error_message && (
            <div className="bg-destructive/10 border border-destructive/20 rounded p-2 text-xs text-destructive">
              <strong>Error:</strong> {String(step.error_message)}
            </div>
          )}
          {inputStr && (
            <div>
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">Input</span>
              <pre className="bg-muted/50 rounded p-2 text-xs mt-0.5 overflow-x-auto max-h-32 overflow-y-auto">{inputStr}</pre>
            </div>
          )}
          {outputStr && (
            <div>
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">Output</span>
              <pre className="bg-muted/50 rounded p-2 text-xs mt-0.5 overflow-x-auto max-h-40 overflow-y-auto">{outputStr}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AgentSessionCollapsible({ session }: { session: AgentSession }) {
  const [open, setOpen] = useState(false);
  return (
    <Card>
      <CardHeader className="pb-0">
        <button onClick={() => setOpen(!open)} className="flex items-center gap-2 text-sm hover:text-foreground text-muted-foreground transition-colors">
          <Cpu size={16} />
          <span className="font-medium">Agent Session Telemetry</span>
          {session.model_name && <span className="text-xs font-mono">{session.model_name}</span>}
          <ChevronRight size={14} className={`transition-transform ${open ? 'rotate-90' : ''}`} />
        </button>
      </CardHeader>
      {open && (
        <CardContent className="pt-3">
          <AgentSessionDetail session={session} />
        </CardContent>
      )}
    </Card>
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
  const hasCompression = s.rca_compression_applied || s.preflight_compression_applied || s.middleware_trim_applied || (s.context_messages_loaded != null && s.context_messages_loaded > 0);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 text-xs">
      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Cpu size={12} /> Model & Routing</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Model" value={s.model_name} />
          <DetailRow label="Provider" value={s.detected_provider} />
          <DetailRow label="Mode" value={s.provider_mode} />
          <DetailRow label="Temperature" value={s.temperature != null ? String(s.temperature) : null} />
          <DetailRow label="Recursion Limit" value={s.recursion_limit != null ? String(s.recursion_limit) : null} />
        </CardContent>
      </Card>

      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Zap size={12} /> Execution</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Duration" value={s.duration_ms != null ? formatDuration(s.duration_ms / 1000) : null} />
          <DetailRow label="TTFT" value={s.time_to_first_token_ms != null ? `${s.time_to_first_token_ms.toLocaleString()}ms` : null} />
          <DetailRow label="Model Turns" value={s.model_turns != null ? String(s.model_turns) : null} />
          <DetailRow label="Tool Calls" value={s.tool_calls_count != null ? String(s.tool_calls_count) : null} />
          <DetailRow label="Tool Errors" value={s.tool_errors_count != null ? String(s.tool_errors_count) : null} highlight={!!s.tool_errors_count} />
          <DetailRow label="Retries" value={s.retry_attempts != null ? String(s.retry_attempts) : null} highlight={!!s.retry_attempts} />
          {s.last_retry_error && <DetailRow label="Last Retry Error" value={s.last_retry_error} highlight />}
        </CardContent>
      </Card>

      <Card className="bg-muted/30">
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Sparkles size={12} /> LLM Usage</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="LLM Calls" value={s.total_llm_calls != null ? String(s.total_llm_calls) : null} />
          <DetailRow label="Input Tokens" value={s.total_input_tokens != null ? s.total_input_tokens.toLocaleString() : null} />
          <DetailRow label="Output Tokens" value={s.total_output_tokens != null ? s.total_output_tokens.toLocaleString() : null} />
          <DetailRow label="Total Cost" value={s.total_cost != null ? `$${Number(s.total_cost).toFixed(4)}` : null} />
        </CardContent>
      </Card>

      {hasCompression && (
        <Card className="bg-muted/30">
          <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><Gauge size={12} /> Context & Compression</CardTitle></CardHeader>
          <CardContent className="px-3 pb-3 space-y-1">
            {(s.context_messages_loaded ?? 0) > 0 && <DetailRow label="Messages Loaded" value={String(s.context_messages_loaded)} />}
            {(s.context_load_ms ?? 0) > 0 && <DetailRow label="Context Load" value={`${s.context_load_ms}ms`} />}
            {s.rca_compression_applied && <DetailRow label="RCA Compression" value={`${s.rca_compression_before} → ${s.rca_compression_after} msgs`} />}
            {s.preflight_compression_applied && <DetailRow label="Preflight Compress" value="Yes" />}
            {s.middleware_trim_applied && <DetailRow label="Middleware Trim" value={`${s.middleware_tokens_before} → ${s.middleware_tokens_after} tokens`} />}
          </CardContent>
        </Card>
      )}

      <Card className={`bg-muted/30 ${hasCompression ? '' : 'md:col-span-2'}`}>
        <CardHeader className="pb-2 pt-3 px-3"><CardTitle className="text-xs flex items-center gap-1.5"><CheckCircle2 size={12} /> Outcome</CardTitle></CardHeader>
        <CardContent className="px-3 pb-3 space-y-1">
          <DetailRow label="Status" value={s.status} />
          {s.error_message && <DetailRow label="Error" value={s.error_message} highlight />}
          {s.placeholder_warning && <DetailRow label="Placeholder Warning" value="AI output contained placeholder tokens" highlight />}
          <DetailRow label="Started" value={s.started_at ? new Date(s.started_at).toLocaleString() : null} />
          <DetailRow label="Completed" value={s.completed_at ? new Date(s.completed_at).toLocaleString() : null} />
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
