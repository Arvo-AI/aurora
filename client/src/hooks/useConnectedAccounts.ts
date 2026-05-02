import { useCallback, useMemo } from 'react';
import { isOvhEnabled } from '@/lib/feature-flags';
import { useQuery } from '@/lib/query';
import {
  CACHE_KEY,
  STALE_TIME,
  fetcher,
  type ConnectedAccountsData,
} from '@/lib/connected-accounts-cache';

export interface ConnectedProvider {
  id: string;
  name: string;
  icon: string;
}

const INFRA_PROVIDERS: Record<string, { name: string; icon: string }> = {
  gcp: { name: 'Google Cloud', icon: '/google-cloud-svgrepo-com.svg' },
  aws: { name: 'AWS', icon: '/aws.ico' },
  azure: { name: 'Azure', icon: '/azure.ico' },
  ovh: { name: 'OVH Cloud', icon: '/ovh.svg' },
  scaleway: { name: 'Scaleway', icon: '/scaleway.svg' },
  tailscale: { name: 'Tailscale', icon: '/tailscale.svg' },
  kubectl: { name: 'Kubernetes', icon: '/kubernetes-svgrepo-com.svg' },
  onprem: { name: 'Instances SSH Access', icon: '/cloud-server-svgrepo-com.svg' },
};

// Stable fallbacks: a fresh `[]`/`{}` on every render cascaded new identities
// through downstream useCallback deps and triggered Maximum-update-depth loops.
const EMPTY_IDS: string[] = [];
const EMPTY_ACCOUNTS: ConnectedAccountsData['accounts'] = {};

/**
 * Single hook for all connected-accounts data.
 *
 * Backed by the global query cache — at most one network request
 * regardless of how many components mount this hook. Retries with
 * exponential backoff, deduplicates in-flight requests, and
 * revalidates on window focus and providerStateChanged events.
 */
export function useConnectedAccounts() {
  const { data, error, isLoading, mutate } = useQuery<ConnectedAccountsData>(
    CACHE_KEY,
    fetcher,
    {
      staleTime: STALE_TIME,
      retryCount: 3,
      retryDelay: 2000,
      revalidateOnFocus: true,
      revalidateOnEvents: ['providerStateChanged', 'providerConnectionAction'],
    },
  );

  // Stabilize providerIds reference for downstream useCallback/useMemo deps.
  const rawIds = data?.providerIds ?? EMPTY_IDS;
  const providerIds = useMemo(
    () => (rawIds === EMPTY_IDS ? EMPTY_IDS : [...rawIds]),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawIds.join('|')],
  );

  const accounts = data?.accounts ?? EMPTY_ACCOUNTS;

  const isProviderConnected = useCallback(
    (id: string) => providerIds.includes(id.toLowerCase()),
    [providerIds],
  );

  const infraProviders = useMemo<ConnectedProvider[]>(
    () =>
      providerIds
        .filter(id => {
          if (!(id in INFRA_PROVIDERS)) return false;
          if (id === 'ovh' && !isOvhEnabled()) return false;
          return true;
        })
        .map(id => ({
          id,
          name: INFRA_PROVIDERS[id].name,
          icon: INFRA_PROVIDERS[id].icon,
        })),
    [providerIds],
  );

  return {
    accounts,
    providerIds,
    isLoading,
    hasError: !!error,
    isProviderConnected,
    infraProviders,
    refetch: mutate,
  };
}
