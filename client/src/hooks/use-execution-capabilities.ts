import { useQuery, type Fetcher } from '@/lib/query';

export interface ExecutionCapabilities {
  canExecute: boolean;
}

interface StatusPayload {
  connectors: Record<string, { connected?: boolean; canExecute?: boolean }>;
}

const fetcher: Fetcher<ExecutionCapabilities> = async (_key, signal) => {
  const res = await fetch('/api/connectors/status', { credentials: 'include', signal });
  if (!res.ok) return { canExecute: false };
  const data: StatusPayload = await res.json();
  const c = data.connectors || {};
  const canExecute = Object.values(c).some(v => v.canExecute === true);
  return { canExecute };
};

export function useExecutionCapabilities() {
  const { data } = useQuery<ExecutionCapabilities>(
    '/api/connectors/status/exec-caps',
    fetcher,
    { staleTime: 60_000, retryCount: 1, revalidateOnFocus: true },
  );
  return data ?? { canExecute: false };
}

export function canExecuteCommand(_command: string, caps: ExecutionCapabilities): boolean {
  return caps.canExecute;
}
