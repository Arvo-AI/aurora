import { useEffect, useSyncExternalStore } from 'react';
import { isOvhEnabled } from '@/lib/feature-flags';
import {
  fetchConnectedAccounts,
  getConnectedAccounts,
  subscribe,
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
  kubectl: { name: 'Kubernetes', icon: '/kubernetes-svgrepo-com.svg' },
};

function getSnapshot() {
  return getConnectedAccounts();
}

function getServerSnapshot() {
  return { accounts: {}, providerIds: [], fetchedAt: 0 };
}

/**
 * Single hook for all connected-accounts data.
 *
 * Every consumer in the app reads from the same in-memory cache
 * (connected-accounts-cache.ts), so there is at most one network
 * request regardless of how many components mount this hook.
 */
export function useConnectedAccounts() {
  const state = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  useEffect(() => {
    fetchConnectedAccounts();
  }, []);

  const isProviderConnected = (id: string) =>
    state.providerIds.includes(id.toLowerCase());

  const infraProviders: ConnectedProvider[] = state.providerIds
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
    accounts: state.accounts,
    providerIds: state.providerIds,
    isLoading: state.fetchedAt === 0,
    isProviderConnected,
    infraProviders,
  };
}
