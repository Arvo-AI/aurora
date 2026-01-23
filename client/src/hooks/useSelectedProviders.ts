/**
 * Shared hook for getting connected providers from database
 * Fetches all connected providers via /api/connected-accounts
 * No longer uses "selected" providers - all connected providers are shown
 */

import { useState, useEffect } from 'react';
import { isOvhEnabled } from '@/lib/feature-flags';

export interface SelectedProvider {
  id: string;
  name: string;
  icon: string;
}

// Only show Infrastructure category connectors in the chat panel
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

  // Fetch connected providers from database via /api/connected-accounts
  useEffect(() => {
    const fetchConnectedProviders = async () => {
      try {
        const response = await fetch('/api/connected-accounts');
        if (!response.ok) {
          console.error('Failed to fetch connected accounts:', response.status);
          setConnectedProviderIds([]);
          return;
        }

        const data = await response.json();
        // Extract provider IDs from the accounts object keys
        // Normalize to lowercase to handle any case mismatches
        const providers = Object.keys(data.accounts || {}).map(p => p.toLowerCase());
        setConnectedProviderIds(providers);
      } catch (error) {
        console.error('Failed to load connected providers:', error);
        setConnectedProviderIds([]);
      }
    };

    fetchConnectedProviders();

    // Listen for provider state changes to refresh
    const handleProviderChange = () => {
      fetchConnectedProviders();
    };
    window.addEventListener('providerStateChanged', handleProviderChange);
    window.addEventListener('providerConnectionAction', handleProviderChange);
    
    return () => {
      window.removeEventListener('providerStateChanged', handleProviderChange);
      window.removeEventListener('providerConnectionAction', handleProviderChange);
    };
  }, []);

  // Map connected providers to display format, excluding disabled features
  // Only show providers that are in PROVIDER_CONFIG (have proper icons/names configured)
  // and are enabled via feature flags
  const connectedProviders: SelectedProvider[] = connectedProviderIds
    .filter(id => {
      // Only include providers that are in PROVIDER_CONFIG (case-insensitive check)
      const normalizedId = id.toLowerCase();
      if (!(normalizedId in PROVIDER_CONFIG)) {
        return false;
      }

      // Check feature flags for providers that can be disabled
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
