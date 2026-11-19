'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight, Radar } from 'lucide-react';
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
  last_observed: string | null;
  last_run_at: string | null;
  retired_reason: string | null;
}

const ORIGIN_WORDS: Record<string, string> = {
  incident: 'Written after an incident',
  miss: 'Written after a problem Aurora missed',
  graph: 'Default check for this kind of asset',
  explore: 'Aurora noticed something while exploring',
  gap: 'Nothing else was monitoring this',
  person: 'Someone asked Aurora to watch this',
};

const STATE_STYLES: Record<CheckState, string> = {
  proven: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  silent: 'bg-zinc-800 text-zinc-400 border-zinc-700/60',
  retired: 'bg-zinc-900 text-zinc-600 border-zinc-800',
};

const STATE_HINTS: Record<CheckState, string> = {
  proven: 'A failure opens a case',
  silent: 'Records results, notifies no one',
  retired: 'No longer runs',
};

const STATUS_DOT: Record<string, string> = {
  pass: 'bg-emerald-400',
  fail: 'bg-red-400',
  error: 'bg-amber-400',
};

const checksFetcher = async (key: string, signal: AbortSignal) => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`Failed to load checks: ${res.status}`);
  const data = await res.json();
  return (data.checks || []) as Check[];
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

function CheckRow({ check }: { readonly check: Check }) {
  const [open, setOpen] = useState(false);
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
                title={STATE_HINTS[check.state]}
                className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${STATE_STYLES[check.state]}`}
              >
                {check.state}
              </span>
            </div>
            <p className="text-xs text-zinc-500 leading-relaxed">{check.prediction}</p>
            <div className="flex items-center gap-3 mt-3 flex-wrap text-xs text-zinc-500">
              <span className="text-zinc-400">{check.asset}</span>
              <span className="flex items-center gap-1.5">
                <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[check.last_status ?? ''] ?? 'bg-zinc-700'}`} />
                {check.last_status ?? 'pending'} · {check.last_run_at ? formatTimeAgo(check.last_run_at) : 'not run yet'}
              </span>
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
            <Field label="Why it exists">
              {origin}{check.origin?.ref ? ` (${check.origin.ref})` : ''}
            </Field>
            <Field label="Kind">{check.pattern} · {check.provider}{check.target ? ` (${check.target})` : ''} · {every(check.interval_seconds)}</Field>
            <Field label="Healthy when">{healthyWhen(check.assertion)}</Field>
            <Field label="Last value seen">{check.last_observed || '—'}</Field>
            <Field label="Record">
              {check.fail_count} failing {check.fail_count === 1 ? 'run' : 'runs'} · {check.error_count} errors · revision {check.revision}
            </Field>
            {check.retired_reason && <Field label="Retired because">{check.retired_reason}</Field>}
          </div>
          <Field label="Command">
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] text-zinc-400 bg-zinc-950/60 border border-zinc-800/80 rounded-lg p-3">{check.command}</pre>
          </Field>
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
  const { data, isLoading, error } = useQuery<Check[]>(
    `/api/detection/checks${showRetired ? '?include_retired=true' : ''}`, checksFetcher, { staleTime: 10_000 },
  );
  const checks = data ?? [];
  const active = checks.filter((c) => c.state !== 'retired');
  const proven = active.filter((c) => c.state === 'proven').length;
  const failing = active.filter((c) => c.last_status === 'fail').length;

  const renderBody = () => {
    if (isLoading) return <div className="text-sm text-zinc-500 py-12 text-center">Loading...</div>;
    if (error) return <div className="text-sm text-red-400 py-12 text-center">Could not load checks.</div>;
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
    return <div className="space-y-3">{checks.map((check) => <CheckRow key={check.id} check={check} />)}</div>;
  };

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">Checks</h2>
          <p className="text-sm text-zinc-500 mt-1">What Aurora watches by itself, why, and how often it has been right</p>
        </div>
        <label className="flex items-center gap-2 text-xs text-zinc-500 cursor-pointer">
          <input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} />
          <span>Show retired</span>
        </label>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <Stat label="Running" value={String(active.length)} />
        <Stat label="Proven" value={String(proven)} sub="A failure opens a case" />
        <Stat label="Failing now" value={String(failing)} />
      </div>

      {renderBody()}
    </div>
  );
}
