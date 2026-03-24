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
};

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

  const providerIds = data?.providerIds ?? [];
  const accounts = data?.accounts ?? {};

  const isProviderConnected = (id: string) =>
    providerIds.includes(id.toLowerCase());

  const infraProviders: ConnectedProvider[] = providerIds
    .filter(id => {
      if (!(id in INFRA_PROVIDERS)) return false;
      if (id === 'ovh' && !isOvhEnabled()) return false;
      return true;
    })
    .map(id => ({
      id,
      name: INFRA_PROVIDERS[id].name,
      icon: INFRA_PROVIDERS[id].icon,
    }));

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
