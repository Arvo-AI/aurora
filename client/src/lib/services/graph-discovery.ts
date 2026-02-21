import { getEnv } from '@/lib/env';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

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
export async function triggerGraphDiscovery(
  userId: string
): Promise<{ task_id: string }> {
  const res = await fetch(`${BACKEND_URL}/api/graph/discover`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-User-ID": userId,
    },
  });
  if (!res.ok) throw new Error("Failed to trigger graph discovery");
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
  userId: string,
  taskId: string
): Promise<DiscoveryStatus> {
  const res = await fetch(
    `${BACKEND_URL}/api/graph/discover/status/${taskId}`,
    {
      headers: { "X-User-ID": userId },
    }
  );
  if (!res.ok) throw new Error("Failed to poll discovery status");
  return res.json();
}
