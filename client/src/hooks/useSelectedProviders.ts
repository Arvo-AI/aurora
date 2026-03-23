/**
 * Shared hook for getting connected providers from database.
 * Reads from a shared in-memory cache (single fetch for all consumers).
 */

import { useState, useEffect } from 'react';
import { isOvhEnabled } from '@/lib/feature-flags';
import {
  fetchConnectedAccounts,
  getConnectedAccounts,
  subscribe,
} from '@/lib/connected-accounts-cache';

export interface SelectedProvider {
  id: string;
  name: string;
  icon: string;
}

const PROVIDER_CONFIG = {
  gcp: { name: 'Google Cloud', icon: '/google-cloud-svgrepo-com.svg' },
  aws: { name: 'AWS', icon: '/aws.ico' },
  azure: { name: 'Azure', icon: '/azure.ico' },
  ovh: { name: 'OVH Cloud', icon: '/ovh.svg' },
  scaleway: { name: 'Scaleway', icon: '/scaleway.svg' },
  kubectl: { name: 'Kubernetes', icon: '/kubernetes-svgrepo-com.svg' }
} as const;

export const useSelectedProviders = () => {
  const [connectedProviderIds, setConnectedProviderIds] = useState<string[]>([]);

  useEffect(() => {
    const sync = () => {
      const { providerIds } = getConnectedAccounts();
      setConnectedProviderIds(providerIds);
    };

    fetchConnectedAccounts().then(sync).catch(() => setConnectedProviderIds([]));

    const unsub = subscribe(sync);
    return unsub;
  }, []);

  const connectedProviders: SelectedProvider[] = connectedProviderIds
    .filter(id => {
      const normalizedId = id.toLowerCase();
      if (!(normalizedId in PROVIDER_CONFIG)) return false;
      if (normalizedId === 'ovh' && !isOvhEnabled()) return false;
      return true;
    })
    .map(id => {
      const normalizedId = id.toLowerCase() as keyof typeof PROVIDER_CONFIG;
      return {
        id: normalizedId,
        name: PROVIDER_CONFIG[normalizedId].name,
        icon: PROVIDER_CONFIG[normalizedId].icon
      };
    });

  return connectedProviders;
};
