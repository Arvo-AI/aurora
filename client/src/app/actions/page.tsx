'use client';

import { useState, useCallback, useMemo } from 'react';
import {
  Play,
  Plus,
  ChevronRight,
  Clock,
  CheckCircle2,
  XCircle,
  ArrowLeft,
  Loader2,
  Workflow,
  AlertTriangle,
  Hash,
  RotateCcw,
  Shield,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useQuery, fetchR } from '@/lib/query';
import { useToast } from '@/hooks/use-toast';
import { formatTimeAgo } from '@/lib/utils/time-format';

interface Action {
  id: string;
  name: string;
  description: string;
  instructions: string;
  trigger_type: 'on_incident' | 'manual' | 'on_schedule';
  trigger_config: Record<string, unknown>;
  mode: 'agent' | 'ask';
  enabled: boolean;
  run_count: number;
  last_run_at: string | null;
  last_run_status: 'success' | 'error' | 'running' | null;
  is_system?: boolean;
  is_modified?: boolean;
}

interface ActionRun {
  id: string;
  status: 'success' | 'error' | 'running' | 'pending';
  trigger_context: Record<string, unknown>;
  started_at: string;
  completed_at: string | null;
  duration_ms?: number;
  chat_session_id: string | null;
  incident_id: string | null;
  error: string | null;
}

interface ActionDetail extends Action {
  created_at: string;
  updated_at: string;
  is_system?: boolean;
  is_modified?: boolean;
}

const actionsFetcher = async (key: string, signal: AbortSignal) => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load actions: ${res.status}`);
  const data = await res.json();
  return data.actions || [];
};

const actionDetailFetcher = async (key: string, signal: AbortSignal) => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load action: ${res.status}`);
  return res.json();
};

// -- Shared style primitives (matching monitor page) --

function getTriggerDescription(type: string): string {
  if (type === 'on_incident') return 'Fires automatically when a new incident comes in.';
  if (type === 'on_schedule') return 'Runs automatically on a recurring interval.';
  return 'Only runs when triggered from the Actions page or Incident Detail page.';
}

function StatCard({ label, value, sub, icon: Icon }: {
  readonly label: string;
  readonly value: string;
  readonly sub?: string;
  readonly icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4 hover:ring-1 hover:ring-white/5 transition-all duration-200">
      <div className="flex items-center gap-2 mb-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-zinc-500" />}
        <span className="text-xs text-zinc-500 font-medium uppercase tracking-wider">{label}</span>
      </div>
      <p className="text-3xl font-semibold tracking-tight text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </p>
      {sub && <p className="text-xs text-zinc-500 mt-1">{sub}</p>}
    </div>
  );
}

function Panel({ title, subtitle, children }: {
  readonly title: string;
  readonly subtitle?: string;
  readonly children: React.ReactNode;
}) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-5">
      <div className="mb-4">
        <h3 className="text-sm font-medium text-zinc-300">{title}</h3>
        {subtitle && <p className="text-xs text-zinc-500 mt-0.5">{subtitle}</p>}
      </div>
      {children}
    </div>
  );
}

function TriggerBadge({ type }: { readonly type: Action['trigger_type'] }) {
  const styles: Record<string, string> = {
    on_incident: 'bg-blue-500/10 text-blue-400',
    manual: 'bg-zinc-500/10 text-zinc-400',
    on_schedule: 'bg-purple-500/10 text-purple-400',
  };
  const labels: Record<string, string> = {
    on_incident: 'On Incident',
    manual: 'Manual',
    on_schedule: 'Scheduled',
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${styles[type] || styles.manual}`}>
      {labels[type] || type}
    </span>
  );
}

function ModeBadge({ mode }: { readonly mode: Action['mode'] }) {
  return mode === 'agent'
    ? <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400">Read-Write</span>
    : <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-zinc-500/10 text-zinc-400">Read-Only</span>;
}

function StatusDot({ status }: { readonly status: ActionRun['status'] }) {
  switch (status) {
    case 'success': return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />;
    case 'error': return <XCircle className="h-3.5 w-3.5 text-red-400" />;
    case 'running': return <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-400" />;
    default: return <Clock className="h-3.5 w-3.5 text-zinc-500" />;
  }
}

// -- List view --

function ActionsListView({ actions, onSelect, onCreate }: {
  readonly actions: Action[];
  readonly onSelect: (a: Action) => void;
  readonly onCreate: () => void;
}) {
  const active = actions.filter(a => a.enabled).length;
  const totalRuns = actions.reduce((s, a) => s + (a.run_count || 0), 0);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Total Actions" value={String(actions.length)} icon={Workflow} />
        <StatCard label="Active" value={String(active)} icon={CheckCircle2} sub="Enabled and triggering" />
        <StatCard label="Total Runs" value={String(totalRuns)} icon={Play} />
      </div>

      {actions.length === 0 ? (
        <Panel title="Configured Actions" subtitle="Background agent tasks that follow your instructions">
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Workflow className="h-8 w-8 text-zinc-700 mb-3" />
            <p className="text-sm text-zinc-500">No actions yet</p>
            <p className="text-xs text-zinc-600 mt-1 mb-4">Create your first action to automate SRE workflows</p>
            <Button variant="outline" size="sm" onClick={onCreate}>
              <Plus className="h-3.5 w-3.5 mr-1.5" /> Create Action
            </Button>
          </div>
        </Panel>
      ) : (
        <div className="space-y-3">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              onClick={() => onSelect(action)}
              className="group w-full text-left bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-5 cursor-pointer hover:border-zinc-700/80 hover:bg-zinc-800/40 transition-all duration-200"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2.5 mb-1">
                    <h3 className="text-sm font-medium text-zinc-100">{action.name}</h3>
                    {action.is_system && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">System</span>
                    )}
                    {!action.enabled && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-zinc-800 text-zinc-500">Disabled</span>
                    )}
                  </div>
                  {action.description && (
                    <p className="text-xs text-zinc-500 leading-relaxed">{action.description}</p>
                  )}
                  <div className="flex items-center gap-3 mt-3">
                    <TriggerBadge type={action.trigger_type} />
                    <ModeBadge mode={action.mode} />
                    {action.last_run_at && (
                      <div className="flex items-center gap-1.5">
                        <StatusDot status={action.last_run_status || 'pending'} />
                        <span className="text-xs text-zinc-500">{formatTimeAgo(action.last_run_at)}</span>
                      </div>
                    )}
                    {action.run_count > 0 && (
                      <span className="text-xs text-zinc-600">{action.run_count} {action.run_count === 1 ? 'run' : 'runs'}</span>
                    )}
                  </div>
                </div>
                <ChevronRight className="h-4 w-4 text-zinc-700 group-hover:text-zinc-400 transition-colors mt-0.5 flex-shrink-0" />
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// -- Detail view --

function ActionDetailView({ actionId, onBack, onEdit }: { readonly actionId: string; readonly onBack: () => void; readonly onEdit: () => void }) {
  const { toast } = useToast();
  const { data, mutate } = useQuery<{ action: ActionDetail; recent_runs: ActionRun[] }>(
    `/api/actions/${actionId}`, actionDetailFetcher, { staleTime: 5_000 }
  );

  const action = data?.action;
  const runs = data?.recent_runs || [];

  const hasActiveRuns = useMemo(
    () => runs.some(r => r.status === 'pending' || r.status === 'running'),
    [runs],
  );

  // Poll while runs are in-flight so the UI updates when they finish
  useQuery<{ action: ActionDetail; recent_runs: ActionRun[] }>(
    hasActiveRuns ? `/api/actions/${actionId}` : null,
    actionDetailFetcher,
    { refreshInterval: 3_000, staleTime: 2_000 },
  );

  const handleToggle = useCallback(async (enabled: boolean) => {
    try {
      await fetchR(`/api/actions/${actionId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      mutate();
    } catch {
      toast({ title: 'Failed to update action', variant: 'destructive' });
    }
  }, [actionId, mutate, toast]);

  const handleRunNow = useCallback(async () => {
    try {
      const res = await fetchR(`/api/actions/${actionId}/run`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast({ title: 'Failed to trigger action', description: body.error || res.statusText, variant: 'destructive' });
        return;
      }
      toast({ title: 'Action triggered', description: 'Background task started.' });
      mutate();
    } catch {
      toast({ title: 'Failed to trigger action', variant: 'destructive' });
    }
  }, [actionId, mutate, toast]);

  const handleDelete = useCallback(async () => {
    if (!confirm('Delete this action? This cannot be undone.')) return;
    try {
      await fetchR(`/api/actions/${actionId}`, { method: 'DELETE' });
      globalThis.dispatchEvent(new Event('actionsStateChanged'));
      onBack();
    } catch {
      toast({ title: 'Failed to delete action', variant: 'destructive' });
    }
  }, [actionId, onBack, toast]);

  const handleRestoreDefault = useCallback(async () => {
    if (!confirm('Restore instructions to the built-in default? Your customizations will be lost.')) return;
    try {
      await fetchR(`/api/actions/${actionId}/restore-default`, { method: 'POST' });
      toast({ title: 'Restored to default instructions' });
      mutate();
    } catch {
      toast({ title: 'Failed to restore default', variant: 'destructive' });
    }
  }, [actionId, mutate, toast]);

  if (!action) {
    return <div className="text-zinc-500 text-sm py-12 text-center">Loading...</div>;
  }

  const succeeded = runs.filter(r => r.status === 'success').length;
  const failed = runs.filter(r => r.status === 'error').length;

  return (
    <div className="space-y-6">
      <div>
        <button onClick={onBack} className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors mb-3">
          <ArrowLeft className="h-3 w-3" /> All Actions
        </button>
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-semibold tracking-tight text-zinc-100">{action.name}</h2>
              {action.is_system && (
                <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex items-center gap-1">
                  <Shield className="h-2.5 w-2.5" /> System
                </span>
              )}
            </div>
            {action.description && <p className="text-sm text-zinc-500 mt-0.5">{action.description}</p>}
            {action.trigger_type === 'on_incident' && (
              <p className="text-xs text-zinc-600 mt-1">Triggered automatically when an incident is resolved from the Incidents page.</p>
            )}
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">Enabled</span>
              <Switch checked={action.enabled} onCheckedChange={handleToggle} />
            </div>
            {action.trigger_type !== 'on_incident' && (
              <button onClick={handleRunNow} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700/50 text-xs font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/80 transition-all">
                <Play className="h-3 w-3" /> Run Now
              </button>
            )}
            <button onClick={onEdit} className="px-3 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700/50 text-xs font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/80 transition-all">
              Edit
            </button>
            {action.is_system && action.is_modified && (
              <button onClick={handleRestoreDefault} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-800 border border-indigo-500/30 text-xs font-medium text-indigo-400 hover:text-indigo-300 hover:bg-indigo-900/20 transition-all">
                <RotateCcw className="h-3 w-3" /> Restore Default
              </button>
            )}
            {!action.is_system && (
              <button onClick={handleDelete} className="px-3 py-1.5 rounded-lg bg-zinc-800 border border-red-900/30 text-xs font-medium text-red-400 hover:text-red-300 hover:bg-red-900/20 transition-all">
                Delete
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Total Runs" value={String(runs.length)} icon={Hash} />
        <StatCard label="Succeeded" value={String(succeeded)} icon={CheckCircle2} />
        <StatCard label="Failed" value={String(failed)} icon={XCircle} />
      </div>

      <Panel title="Configuration">
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <TriggerBadge type={action.trigger_type} />
            <ModeBadge mode={action.mode} />
          </div>
          <div>
            <p className="text-xs text-zinc-500 font-medium uppercase tracking-wider mb-2">Instructions</p>
            <div className="bg-zinc-950/50 border border-zinc-800/50 rounded-lg p-3 text-sm text-zinc-300 whitespace-pre-wrap leading-relaxed">
              {action.instructions}
            </div>
          </div>
          {action.mode === 'agent' && (
            <div className="flex items-start gap-2 text-xs text-amber-400 bg-amber-500/5 border border-amber-500/10 rounded-lg px-3 py-2">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" />
              <span>Agent mode: Aurora can execute commands, modify Terraform, and open PRs. All actions are logged.</span>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Run History" subtitle="Recent executions of this action">
        {runs.length === 0 ? (
          <p className="text-sm text-zinc-600 py-4 text-center">No runs yet</p>
        ) : (
          <div className="overflow-hidden rounded-lg border border-zinc-800/60">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                  <th className="text-left px-4 py-2.5 font-medium">Status</th>
                  <th className="text-left px-4 py-2.5 font-medium">Started</th>
                  <th className="text-left px-4 py-2.5 font-medium">Duration</th>
                  <th className="text-left px-4 py-2.5 font-medium w-20"></th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className={`border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150 ${run.status === 'error' ? 'bg-red-500/[0.03]' : ''}`}>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <StatusDot status={run.status} />
                        <span className="text-xs text-zinc-400 capitalize">{run.status}</span>
                      </div>
                      {run.error && <p className="text-xs text-red-400/70 mt-0.5 truncate max-w-xs">{run.error}</p>}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-zinc-500" style={{ fontVariantNumeric: 'tabular-nums' }}>
                      {run.started_at ? formatTimeAgo(run.started_at) : '-'}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-zinc-500" style={{ fontVariantNumeric: 'tabular-nums' }}>
                      {run.duration_ms !== null && run.duration_ms !== undefined ? `${(run.duration_ms / 1000).toFixed(1)}s` : '-'}
                    </td>
                    <td className="px-4 py-2.5">
                      {run.chat_session_id && (run.status === 'success' || run.status === 'error') && (
                        <a href={`/chat?sessionId=${run.chat_session_id}`} className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors">
                          View Chat
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

// -- Form view (create + edit) --

function ActionFormView({ onBack, onSaved, action }: {
  readonly onBack: () => void;
  readonly onSaved: () => void;
  readonly action?: ActionDetail;
}) {
  const isEdit = !!action;
  const isSystemAction = action?.is_system || false;
  const submitLabel = isEdit ? 'Save Changes' : 'Create Action';
  const submittingLabel = isEdit ? 'Saving...' : 'Creating...';
  const [name, setName] = useState(action?.name || '');
  const [description, setDescription] = useState(action?.description || '');
  const [instructions, setInstructions] = useState(action?.instructions || '');
  const [triggerType, setTriggerType] = useState(action?.trigger_type || 'manual');
  const [mode, setMode] = useState(action?.mode || 'agent');
  const [incidentTiming, setIncidentTiming] = useState<'immediate' | 'after_rca' | 'resolved'>(
    () => (action?.trigger_config?.timing as 'immediate' | 'after_rca' | 'resolved') || 'after_rca'
  );
  const [intervalValue, setIntervalValue] = useState(() => {
    const s = Number(action?.trigger_config?.interval_seconds || 3600);
    if (s >= 86400 && s % 86400 === 0) return s / 86400;
    if (s >= 3600 && s % 3600 === 0) return s / 3600;
    return s / 60;
  });
  const [intervalUnit, setIntervalUnit] = useState<'minutes' | 'hours' | 'days'>(() => {
    const s = Number(action?.trigger_config?.interval_seconds || 3600);
    if (s >= 86400 && s % 86400 === 0) return 'days';
    if (s >= 3600 && s % 3600 === 0) return 'hours';
    return 'minutes';
  });
  const [submitting, setSubmitting] = useState(false);
  const { toast } = useToast();

  const getIntervalSeconds = () => {
    const multipliers = { minutes: 60, hours: 3600, days: 86400 };
    return Math.round(intervalValue * multipliers[intervalUnit]);
  };
  const intervalTooLow = triggerType === 'on_schedule' && getIntervalSeconds() < 300;

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        name, description: description || undefined, instructions,
        trigger_type: triggerType, mode,
      };
      if (triggerType === 'on_schedule') {
        body.trigger_config = { interval_seconds: getIntervalSeconds() };
      } else if (triggerType === 'on_incident') {
        body.trigger_config = { timing: incidentTiming };
      } else {
        body.trigger_config = {};
      }

      const url = isEdit ? `/api/actions/${action.id}` : '/api/actions';
      const method = isEdit ? 'PUT' : 'POST';
      const res = await fetchR(url, {
        method, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast({ title: `Failed to ${isEdit ? 'update' : 'create'} action`, description: err.error || 'Unknown error', variant: 'destructive' });
        return;
      }
      toast({ title: isEdit ? 'Action updated' : 'Action created' });
      globalThis.dispatchEvent(new Event('actionsStateChanged'));
      onSaved();
    } catch {
      toast({ title: `Failed to ${isEdit ? 'update' : 'create'} action`, variant: 'destructive' });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <button onClick={onBack} className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors mb-3">
          <ArrowLeft className="h-3 w-3" /> {isEdit ? 'Back' : 'All Actions'}
        </button>
        <h2 className="text-xl font-semibold tracking-tight text-zinc-100">{isEdit ? 'Edit Action' : 'Create Action'}</h2>
        <p className="text-sm text-zinc-500 mt-0.5">Define natural language instructions that Aurora executes as a background agent task.</p>
      </div>

      <div className="grid grid-cols-[1fr_320px] gap-6">
        <div className="space-y-5">
          {isSystemAction && (
            <div className="flex items-start gap-2 text-xs text-indigo-400 bg-indigo-500/5 border border-indigo-500/10 rounded-lg px-3 py-2">
              <Shield className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" />
              <span>This is a built-in system action. You can customize the instructions — use &quot;Restore Default&quot; to revert.</span>
            </div>
          )}
          <Panel title="Details">
            <div className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="action-name" className="text-xs text-zinc-400">Name</Label>
                <Input id="action-name" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Mute noisy Datadog alerts via Terraform" disabled={isSystemAction} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="action-desc" className="text-xs text-zinc-400">Description</Label>
                <Input id="action-desc" value={description} onChange={e => setDescription(e.target.value)} placeholder="Short summary of what this action does" />
              </div>
            </div>
          </Panel>

          <Panel title="Agent Instructions" subtitle="What should Aurora do when this action runs?">
            <div className="space-y-3">
              <Textarea
                value={instructions}
                onChange={e => setInstructions(e.target.value)}
                placeholder={"Write natural language instructions for Aurora...\n\ne.g. Find the Terraform config that defines this Datadog monitor in our GitHub repo. Modify it to add a mute/downtime rule or adjust the threshold. Open a PR with the change and explain why the alert was noisy."}
                rows={10}
                className="bg-zinc-950/50 border-zinc-800/50 text-zinc-200 placeholder:text-zinc-600"
              />
              <p className="text-xs text-zinc-600">
                Aurora receives these instructions along with context about the triggering event (incident details, alert data) and executes them using your connected tools.
              </p>
            </div>
          </Panel>
        </div>

        <div className="space-y-5">
          <Panel title="Settings">
            <div className="space-y-4">
              <div className="space-y-2">
                <Label className="text-xs text-zinc-400">Trigger</Label>
                <Select value={triggerType} onValueChange={setTriggerType} disabled={isSystemAction}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="manual">Manual only</SelectItem>
                    <SelectItem value="on_incident">On Incident</SelectItem>
                    <SelectItem value="on_schedule">On Schedule</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-zinc-600">
                  {getTriggerDescription(triggerType)}
                </p>
              </div>

              {triggerType === 'on_incident' && (
                <div className="space-y-2">
                  <Label className="text-xs text-zinc-400">When to trigger</Label>
                  <Select value={incidentTiming} onValueChange={(v) => setIncidentTiming(v as 'immediate' | 'after_rca' | 'resolved')} disabled={isSystemAction}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="immediate">On incident creation</SelectItem>
                      <SelectItem value="after_rca">After RCA completes</SelectItem>
                      <SelectItem value="resolved">On incident resolved</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              )}

              {triggerType === 'on_schedule' && (
                <div className="space-y-2">
                  <Label className="text-xs text-zinc-400">Run every</Label>
                  <div className="flex gap-2">
                    <Input
                      type="number"
                      min={1}
                      value={intervalValue}
                      onChange={e => setIntervalValue(Math.max(1, Number(e.target.value)))}
                      className="w-20"
                    />
                    <Select value={intervalUnit} onValueChange={v => setIntervalUnit(v as 'minutes' | 'hours' | 'days')}>
                      <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="minutes">minutes</SelectItem>
                        <SelectItem value="hours">hours</SelectItem>
                        <SelectItem value="days">days</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  {getIntervalSeconds() < 300 && (
                    <p className="text-xs text-red-400">Minimum interval is 5 minutes.</p>
                  )}
                </div>
              )}

              <div className="space-y-2">
                <Label className="text-xs text-zinc-400">Execution Mode</Label>
                <Select value={mode} onValueChange={setMode}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="agent">Agent (read-write)</SelectItem>
                    <SelectItem value="ask">Ask (read-only)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {mode === 'agent' && (
                <div className="flex items-start gap-2 text-xs text-amber-400 bg-amber-500/5 border border-amber-500/10 rounded-lg px-3 py-2">
                  <AlertTriangle className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" />
                  <span>Agent mode allows Aurora to execute commands, modify infrastructure, and open PRs.</span>
                </div>
              )}
            </div>
          </Panel>

          <div className="flex gap-2">
            <button
              disabled={!name.trim() || !instructions.trim() || submitting || intervalTooLow}
              onClick={handleSubmit}
              className="flex-1 px-3 py-2 rounded-lg bg-zinc-100 text-zinc-900 text-xs font-medium hover:bg-white transition-all disabled:opacity-30 disabled:cursor-not-allowed"
            >
              {submitting ? submittingLabel : submitLabel}
            </button>
            <button onClick={onBack} className="px-3 py-2 rounded-lg bg-zinc-800 border border-zinc-700/50 text-xs font-medium text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/80 transition-all">
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// -- Content (used by SettingsModal) --

export function ActionsContent() {
  const [selectedActionId, setSelectedActionId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editingActionId, setEditingActionId] = useState<string | null>(null);

  const { data: rawActions, isLoading, mutate } = useQuery<Action[] | { actions: Action[] }>(
    '/api/actions', actionsFetcher, { staleTime: 10_000 }
  );
  const actions: Action[] = Array.isArray(rawActions) ? rawActions : (rawActions?.actions ?? []);

  const renderView = () => {
    if (editingActionId) {
      return <EditActionWrapper actionId={editingActionId} onBack={() => { setEditingActionId(null); setSelectedActionId(editingActionId); }} onSaved={() => { setEditingActionId(null); mutate(); }} />;
    }
    if (createOpen) {
      return <ActionFormView onBack={() => setCreateOpen(false)} onSaved={() => { setCreateOpen(false); mutate(); }} />;
    }
    if (selectedActionId) {
      return <ActionDetailView actionId={selectedActionId} onBack={() => { setSelectedActionId(null); mutate(); }} onEdit={() => { setEditingActionId(selectedActionId); setSelectedActionId(null); }} />;
    }
    if (isLoading) {
      return <div className="text-sm text-zinc-500 py-12 text-center">Loading...</div>;
    }
    return <ActionsListView actions={actions} onSelect={(a) => setSelectedActionId(a.id)} onCreate={() => setCreateOpen(true)} />;
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">Actions</h2>
          <p className="text-sm text-zinc-500 mt-1">Background agent tasks that follow your instructions</p>
        </div>
        {!createOpen && !selectedActionId && !editingActionId && (
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700/50 text-xs font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/80 transition-all"
          >
            <Plus className="h-3.5 w-3.5" /> Create Action
          </button>
        )}
      </div>

      {renderView()}
    </div>
  );
}

// -- Page (redirects to settings) --

export default function ActionsPage() {
  return <ActionsContent />;
}

function EditActionWrapper({ actionId, onBack, onSaved }: { readonly actionId: string; readonly onBack: () => void; readonly onSaved: () => void }) {
  const { data } = useQuery<{ action: ActionDetail; recent_runs: ActionRun[] }>(
    `/api/actions/${actionId}`, actionDetailFetcher, { staleTime: 5_000 }
  );
  if (!data?.action) return <div className="text-sm text-zinc-500 py-12 text-center">Loading...</div>;
  return <ActionFormView action={data.action} onBack={onBack} onSaved={onSaved} />;
}
