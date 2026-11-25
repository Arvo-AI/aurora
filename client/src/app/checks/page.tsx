'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight, Radar } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useQuery } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';

type CheckState = 'silent' | 'proven' | 'retired';

interface Check {
  id: string;
  name: string;
  state: CheckState;
  revision: number;
  origin: { kind?: string; ref?: string };
  prediction: string;
  asset: string;
  pattern: string;
  provider: string;
  target: string;
  command: string;
  assertion: { expect: string; op: string; value: string | number | null };
  interval_seconds: number;
  run_count: number;
  fail_count: number;
  error_count: number;
  consecutive_errors: number;
  hit_count: number;
  false_fire_count: number;
  last_status: 'pass' | 'fail' | 'error' | null;
  last_observed?: string | null;
  last_run_at: string | null;
  retired_reason: string | null;
  person_vouched?: boolean;
  paused_reason?: string | null;
}

const ORIGIN_WORDS: Record<string, string> = {
  incident: 'Written after an incident',
  miss: 'Written after a problem Aurora missed',
  graph: 'Default check for this kind of asset',
  explore: 'Aurora noticed something while exploring',
  gap: 'Nothing else was monitoring this',
  person: 'Someone asked Aurora to watch this',
};

// What the badge shows is the check's health, in the words any monitor uses.
// Whether a firing check may open a case is said separately, in a sentence.
type Health = 'ok' | 'firing' | 'error' | 'pending' | 'paused' | 'retired';

const HEALTH_LABEL: Record<Health, string> = {
  ok: 'OK', firing: 'Firing', error: 'Error', pending: 'Pending', paused: 'Paused', retired: 'Retired',
};

const HEALTH_STYLES: Record<Health, string> = {
  ok: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  firing: 'bg-red-500/10 text-red-400 border-red-500/20',
  error: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  pending: 'bg-zinc-800 text-zinc-400 border-zinc-700/60',
  paused: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
  retired: 'bg-zinc-900 text-zinc-600 border-zinc-800',
};

const HEALTH_HINTS: Record<Health, string> = {
  ok: 'The last run was healthy',
  firing: 'The last run found the problem this check looks for',
  error: 'The check itself could not run or could not read its output',
  pending: 'Not run yet',
  paused: 'Not running until an editor resumes it',
  retired: 'No longer runs',
};

function healthOf(check: Check): Health {
  if (check.state === 'retired') return 'retired';
  if (check.paused_reason) return 'paused';
  if (check.last_status === 'fail') return 'firing';
  if (check.last_status === 'error') return 'error';
  return check.last_status === 'pass' ? 'ok' : 'pending';
}

function onFiring(check: Check): string {
  if (check.state === 'proven' && check.person_vouched) return 'Opens an incident when it fires, because a person asked for that';
  if (check.state === 'proven') return 'Opens an incident when it fires';
  if (check.state === 'silent') return 'Records only for now: it has not caught enough real incidents to notify anyone';
  return 'Stopped';
}

interface ChecksResponse {
  enabled: boolean;
  checks: Check[];
  can_edit: boolean;
}

const checksFetcher = async (key: string, signal: AbortSignal): Promise<ChecksResponse> => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load checks: ${res.status}`);
  const data = await res.json();
  return { enabled: data.enabled !== false, checks: data.checks || [], can_edit: Boolean(data.can_edit) };
};

function every(seconds: number): string {
  if (seconds % 3600 === 0) return `every ${seconds / 3600}h`;
  return `every ${Math.round(seconds / 60)}m`;
}

const OP_WORDS: Record<string, string> = {
  lt: 'is below', lte: 'is at most', gt: 'is above', gte: 'is at least', eq: 'is', ne: 'is not',
  contains: 'contains', not_contains: 'does not contain', empty: 'is empty', not_empty: 'is not empty',
};

function healthyWhen(a: Check['assertion']): string {
  const subject = a.expect === 'lines' ? 'number of output lines' : 'output';
  const words = OP_WORDS[a.op] ?? a.op;
  return a.value === null || a.value === undefined ? `${subject} ${words}` : `${subject} ${words} ${a.value}`;
}

function Stat({ label, value, sub }: { readonly label: string; readonly value: string; readonly sub?: string }) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4">
      <p className="text-xs text-zinc-500">{label}</p>
      <p className="text-2xl font-semibold text-zinc-100 mt-1">{value}</p>
      {sub && <p className="text-xs text-zinc-600 mt-1">{sub}</p>}
    </div>
  );
}

function Field({ label, children }: { readonly label: string; readonly children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wide text-zinc-600 mb-1">{label}</p>
      <div className="text-xs text-zinc-300">{children}</div>
    </div>
  );
}

function CheckRow({ check, canEdit, onChanged }: {
  readonly check: Check;
  readonly canEdit: boolean;
  readonly onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState('');
  const [trusting, setTrusting] = useState(false);
  const health = healthOf(check);

  const act = async (what: 'vouch' | 'resume') => {
    setTrusting(true);
    setStopError('');
    try {
      const res = await fetch(`/api/detection/checks/${check.id}/${what}`, { method: 'POST', credentials: 'include' });
      if (!res.ok) throw new Error(String(res.status));
      onChanged();
    } catch {
      setStopError('Could not change this check.');
    } finally {
      setTrusting(false);
    }
  };

  const stop = async () => {
    setStopping(true);
    setStopError('');
    try {
      const res = await fetch(`/api/detection/checks/${check.id}/retire`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'Stopped from the Checks page.' }),
      });
      if (!res.ok) throw new Error(String(res.status));
      onChanged();
    } catch {
      setStopError('Could not stop this check.');
    } finally {
      setStopping(false);
      setConfirming(false);
    }
  };
  const origin = ORIGIN_WORDS[check.origin?.kind ?? ''] ?? 'Origin not recorded';
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full text-left p-5 hover:bg-zinc-800/40 rounded-xl transition-colors"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2.5 mb-1 flex-wrap">
              <h3 className="text-sm font-medium text-zinc-100">{check.name}</h3>
              <span
                title={HEALTH_HINTS[health]}
                className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${HEALTH_STYLES[health]}`}
              >
                {HEALTH_LABEL[health]}
              </span>
            </div>
            <p className="text-xs text-zinc-500 leading-relaxed">{check.prediction}</p>
            <div className="flex items-center gap-3 mt-3 flex-wrap text-xs text-zinc-500">
              <span className="text-zinc-400">{check.asset}</span>
              <span>{check.last_run_at ? `checked ${formatTimeAgo(check.last_run_at)}` : 'not run yet'}</span>
              <span className="text-zinc-600">{check.run_count} {check.run_count === 1 ? 'run' : 'runs'}</span>
              <span className="text-zinc-600">{check.hit_count} real · {check.false_fire_count} false</span>
            </div>
          </div>
          <Chevron className="h-4 w-4 text-zinc-600 mt-0.5 flex-shrink-0" />
        </div>
      </button>

      {open && (
        <div className="px-5 pb-5 pt-1 space-y-4 border-t border-zinc-800/80">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-4">
            <Field label="When it fires">{onFiring(check)}</Field>
            <Field label="Why it exists">
              {origin}{check.origin?.ref ? ` (${check.origin.ref})` : ''}
            </Field>
            <Field label="Kind">{check.pattern} · {check.provider}{check.target ? ` (${check.target})` : ''} · {every(check.interval_seconds)}</Field>
            <Field label="Healthy when">{healthyWhen(check.assertion)}</Field>
            {check.last_observed !== undefined && (
              <Field label="Last value seen">{check.last_observed || '—'}</Field>
            )}
            <Field label="Record">
              {check.fail_count} failing {check.fail_count === 1 ? 'run' : 'runs'} · {check.error_count} errors · revision {check.revision}
            </Field>
            {check.retired_reason && <Field label="Retired because">{check.retired_reason}</Field>}
          </div>
          <Field label="Command">
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] text-zinc-400 bg-zinc-950/60 border border-zinc-800/80 rounded-lg p-3">{check.command}</pre>
          </Field>
          {canEdit && check.state !== 'retired' && (
            <div className="flex items-center gap-2">
              {confirming ? (
                <>
                  <span className="text-xs text-zinc-400">Stop this check? Its record is kept and Aurora will not recreate it.</span>
                  <Button variant="destructive" size="sm" disabled={stopping} onClick={stop}>
                    {stopping ? 'Stopping...' : 'Stop check'}
                  </Button>
                  <Button variant="outline" size="sm" disabled={stopping} onClick={() => setConfirming(false)}>
                    Cancel
                  </Button>
                </>
              ) : (
                <>
                  {check.paused_reason && (
                    <Button
                      variant="outline" size="sm" disabled={trusting} onClick={() => act('resume')}
                      title="It runs again on its schedule, with your access."
                    >
                      {trusting ? 'Saving...' : 'Resume this check'}
                    </Button>
                  )}
                  {check.state === 'silent' && !check.paused_reason && (
                    <Button
                      variant="outline" size="sm" disabled={trusting} onClick={() => act('vouch')}
                      title="You vouch for this check: it opens an incident the next time it fires, without waiting to earn that from its record. Two fires in a row on nothing turn this back off."
                    >
                      {trusting ? 'Saving...' : 'Open incidents when it fires'}
                    </Button>
                  )}
                  <Button variant="outline" size="sm" onClick={() => setConfirming(true)}>Stop this check</Button>
                </>
              )}
              {stopError && <span className="text-xs text-red-400">{stopError}</span>}
            </div>
          )}
          {check.paused_reason && check.state !== 'retired' && (
            <p className="text-xs text-sky-400">Paused. {check.paused_reason}</p>
          )}
          {check.consecutive_errors >= 3 && (
            <p className="text-xs text-amber-400">
              This check has not been able to run {check.consecutive_errors} times in a row. An error is never counted as healthy.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default function ChecksPage() {
  const [showRetired, setShowRetired] = useState(false);
  const { data, isLoading, error, mutate } = useQuery<ChecksResponse>(
    `/api/detection/checks${showRetired ? '?include_retired=true' : ''}`,
    checksFetcher,
    // No retries: the next poll is the retry. A retry sequence would be cancelled by
    // the poll and restarted, leaving a failing page on "Loading..." for good.
    { staleTime: 10_000, refreshInterval: 30_000, retryCount: 0 },
  );
  const checks = data?.checks ?? [];
  const canEdit = data?.can_edit ?? false;
  // A paused check is not running: it waits for an editor to resume it.
  const active = checks.filter((c) => c.state !== 'retired' && !c.paused_reason);
  const firing = active.filter((c) => c.last_status === 'fail').length;
  const erroring = active.filter((c) => c.last_status === 'error').length;

  const renderBody = () => {
    if (isLoading) return <div className="text-sm text-zinc-500 py-12 text-center">Loading...</div>;
    if (error) return <div className="text-sm text-red-400 py-12 text-center">Could not load checks.</div>;
    if (data && !data.enabled) {
      return (
        <div className="text-sm text-zinc-500 py-12 text-center">
          Checks are turned off on this install (DETECTION_CHECKS_ENABLED).
        </div>
      );
    }
    if (checks.length === 0) {
      return (
        <div className="flex flex-col items-center justify-center py-16 text-center border border-zinc-800/80 rounded-xl bg-zinc-900/60">
          <Radar className="h-8 w-8 text-zinc-700 mb-3" />
          <p className="text-sm text-zinc-500">Aurora is not running any checks yet</p>
          <p className="text-xs text-zinc-600 mt-1 max-w-sm">
            Aurora writes a check when an investigation shows what would have caught a problem earlier. You can also ask it to watch something.
          </p>
        </div>
      );
    }
    return (
      <div className="space-y-3">
        {checks.map((check) => (
          <CheckRow key={check.id} check={check} canEdit={canEdit} onChanged={() => mutate()} />
        ))}
      </div>
    );
  };

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">Checks</h2>
          <p className="text-sm text-zinc-500 mt-1">What Aurora watches by itself, and why</p>
        </div>
        <div className="flex items-center gap-2">
          <Switch id="show-retired" checked={showRetired} onCheckedChange={setShowRetired} />
          <Label htmlFor="show-retired" className="text-xs text-zinc-500">Show retired</Label>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <Stat label="Running" value={String(active.length)} />
        <Stat label="Firing" value={String(firing)} sub="Found the problem they look for" />
        <Stat label="Errors" value={String(erroring)} sub="Could not run or read their output" />
      </div>

      {renderBody()}
    </div>
  );
}
