export interface AuditLabels {
  sources: Record<string, string>;
  issues: Record<string, string>;
  statuses: Record<string, string>;
}

export const ISSUE_STYLES: Record<string, { text: string; badge: string }> = {
  missing_permission: { text: 'text-amber-400', badge: 'bg-amber-500/10 text-amber-300' },
  blocked_command: { text: 'text-violet-400', badge: 'bg-violet-500/10 text-violet-300' },
  not_connected: { text: 'text-sky-400', badge: 'bg-sky-500/10 text-sky-300' },
  timeout: { text: 'text-orange-400', badge: 'bg-orange-500/10 text-orange-300' },
  unavailable: { text: 'text-rose-400', badge: 'bg-rose-500/10 text-rose-300' },
  other: { text: 'text-zinc-400', badge: 'bg-zinc-700/40 text-zinc-300' },
};

export const SOURCE_STYLES: Record<string, string> = {
  web: 'bg-zinc-500/10 text-zinc-300',
  slack: 'bg-fuchsia-500/10 text-fuchsia-300',
  google_chat: 'bg-emerald-500/10 text-emerald-300',
  teams: 'bg-indigo-500/10 text-indigo-300',
  rca: 'bg-red-500/10 text-red-300',
  action: 'bg-cyan-500/10 text-cyan-300',
  scheduled: 'bg-teal-500/10 text-teal-300',
  background: 'bg-zinc-600/20 text-zinc-400',
};

export const STATUS_STYLES: Record<string, string> = {
  completed: 'text-zinc-400',
  failed: 'text-red-400',
  in_progress: 'text-sky-400',
};

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
