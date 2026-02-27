/**
 * Shared time formatting utilities.
 *
 * Consolidated from jenkins/auth/page.tsx, incidents.ts, and kubectl/manage/page.tsx.
 */

export function formatTimeAgo(dateOrString: Date | string): string {
  const date = typeof dateOrString === "string" ? new Date(dateOrString) : dateOrString;
  const diffMs = Date.now() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remainingSecs = secs % 60;
  if (mins < 60) return remainingSecs > 0 ? `${mins}m ${remainingSecs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;
  return remainingMins > 0 ? `${hours}h ${remainingMins}m` : `${hours}h`;
}

export function formatDurationFromStart(startTime: string): string {
  const diffMs = Date.now() - new Date(startTime).getTime();
  return formatDuration(diffMs);
}
