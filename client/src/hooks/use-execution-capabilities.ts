import { useQuery, type Fetcher } from '@/lib/query';

export interface ExecutionCapabilities {
  /** True if at least one connected provider can execute write commands. */
  canExecute: boolean;
  /** Per-provider execution capability, keyed by normalized provider id. */
  byProvider: Record<string, boolean>;
}

interface StatusPayload {
  connectors: Record<string, { connected?: boolean; canExecute?: boolean }>;
}

const fetcher: Fetcher<ExecutionCapabilities> = async (_key, signal) => {
  const res = await fetch('/api/connectors/status', { credentials: 'include', signal });
  if (!res.ok) return { canExecute: false, byProvider: {} };
  const data: StatusPayload = await res.json();
  const c = data.connectors || {};
  const byProvider: Record<string, boolean> = {};
  for (const [provider, v] of Object.entries(c)) {
    byProvider[provider] = v.canExecute === true;
  }
  const canExecute = Object.values(byProvider).some(Boolean);
  return { canExecute, byProvider };
};

export function useExecutionCapabilities() {
  const { data } = useQuery<ExecutionCapabilities>(
    '/api/connectors/status/exec-caps',
    fetcher,
    { staleTime: 60_000, retryCount: 1, revalidateOnFocus: true },
  );
  return data ?? { canExecute: false, byProvider: {} };
}

/**
 * Map a command to the provider whose capability gates it. Mirrors the backend
 * _normalize_cloud_exec_provider (cloud_exec_tool.py): the leading CLI binary
 * determines which connector must be write-capable.
 */
export function providerForCommand(command: string): string | null {
  const bin = command.trim().split(/\s+/)[0]?.toLowerCase();
  if (!bin) return null;
  switch (bin) {
    case 'aws':
      return 'aws';
    case 'gcloud':
    case 'gsutil':
    case 'bq':
      return 'gcp';
    case 'az':
      return 'azure';
    case 'kubectl':
    case 'helm':
      return 'kubectl';
    default:
      return null;
  }
}

/**
 * Whether a specific command can be executed. Matches the command to its
 * provider and checks THAT provider's capability rather than a global OR, so a
 * write-capable AWS connection doesn't make a read-only GCP command show "Run".
 * Commands with no recognized provider prefix (curl, jq, python3, sed, …) are
 * not gated on a cloud connector and fall back to the global flag.
 */
export function canExecuteCommand(command: string, caps: ExecutionCapabilities): boolean {
  const provider = providerForCommand(command);
  if (provider === null) return caps.canExecute;
  return caps.byProvider[provider] === true;
}
