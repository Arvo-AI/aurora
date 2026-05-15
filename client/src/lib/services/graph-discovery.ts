/** Provider IDs that support graph discovery. */
export const GRAPH_DISCOVERY_PROVIDERS = [
  "gcp",
  "aws",
  "azure",
  "ovh",
  "scaleway",
  "tailscale",
  "kubectl",
] as const;

/** Trigger a full graph discovery run for the user. */
export async function triggerGraphDiscovery(): Promise<{ task_id: string }> {
  const res = await fetch('/api/proxy/graph/discover', { method: 'POST' });
  if (!res.ok) throw new Error('Failed to trigger graph discovery');
  return res.json();
}

export interface DiscoveryStatus {
  state: string;
  status: string;
  complete: boolean;
  error?: boolean;
  result?: Record<string, unknown>;
}

/** Poll the status of an in-flight discovery task. */
export async function pollDiscoveryStatus(
  taskId: string,
): Promise<DiscoveryStatus> {
  const res = await fetch(`/api/proxy/graph/discover/status/${taskId}`);
  if (!res.ok) throw new Error('Failed to poll discovery status');
  return res.json();
}
