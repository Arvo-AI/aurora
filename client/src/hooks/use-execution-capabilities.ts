import { useQuery, type Fetcher } from '@/lib/query';

export interface ExecutionCapabilities {
  aws: boolean;
  gcp: boolean;
  kubectl: boolean;
  azure: boolean;
  terraform: boolean;
}

interface StatusPayload {
  connectors: Record<string, { connected?: boolean; canExecute?: boolean }>;
}

const fetcher: Fetcher<ExecutionCapabilities> = async (_key, signal) => {
  const res = await fetch('/api/connectors/status', { credentials: 'include', signal });
  if (!res.ok) return { aws: false, gcp: false, kubectl: false, azure: false, terraform: false };
  const data: StatusPayload = await res.json();
  const c = data.connectors || {};
  return {
    aws: c.aws?.canExecute === true,
    gcp: c.gcp?.canExecute === true,
    kubectl: c.kubectl?.canExecute === true,
    azure: c.azure?.canExecute === true,
    terraform: c.aws?.canExecute === true || c.gcp?.canExecute === true || c.azure?.canExecute === true,
  };
};

export function useExecutionCapabilities() {
  const { data } = useQuery<ExecutionCapabilities>(
    '/api/connectors/status/exec-caps',
    fetcher,
    { staleTime: 60_000, retryCount: 1, revalidateOnFocus: true },
  );
  return data ?? { aws: false, gcp: false, kubectl: false, azure: false, terraform: false };
}

const PROVIDER_PATTERNS: Array<{ provider: keyof ExecutionCapabilities; pattern: RegExp }> = [
  { provider: 'aws', pattern: /^\s*aws\s/ },
  { provider: 'gcp', pattern: /^\s*gcloud\s/ },
  { provider: 'kubectl', pattern: /^\s*(kubectl|helm)\s/ },
  { provider: 'azure', pattern: /^\s*az\s/ },
  { provider: 'terraform', pattern: /^\s*terraform\s/ },
];

export function canExecuteCommand(command: string, caps: ExecutionCapabilities): boolean {
  for (const { provider, pattern } of PROVIDER_PATTERNS) {
    if (pattern.test(command)) return caps[provider];
  }
  return true;
}
