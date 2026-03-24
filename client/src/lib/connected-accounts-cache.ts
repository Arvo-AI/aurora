/**
 * Connected-accounts constants and fetcher.
 *
 * The actual caching, retry, dedup, and subscription logic lives
 * entirely in queryClient (lib/query.ts). This module only defines
 * the cache key, fetcher, and thin imperative helpers for non-React
 * callers (e.g. ProviderPolling).
 */

import { queryClient, type Fetcher } from '@/lib/query';

// ─── Types ──────────────────────────────────────────────────────────

export interface ConnectedAccountsData {
  accounts: Record<string, any>;
  providerIds: string[];
}

// ─── Constants ──────────────────────────────────────────────────────

export const CACHE_KEY = '/api/connected-accounts';
export const STALE_TIME = 5_000;
const EMPTY: ConnectedAccountsData = { accounts: {}, providerIds: [] };

// ─── Fetcher ────────────────────────────────────────────────────────

export const fetcher: Fetcher<ConnectedAccountsData> = async (_key, signal) => {
  const res = await fetch(CACHE_KEY, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`connected-accounts ${res.status}`);
  const data = await res.json();
  const accounts = data.accounts || {};
  const providerIds = Object.keys(accounts).map(k => k.toLowerCase());
  return { accounts, providerIds };
};

// ─── Imperative API (for non-React code) ────────────────────────────

export function getConnectedAccounts(): ConnectedAccountsData {
  return queryClient.read<ConnectedAccountsData>(CACHE_KEY) ?? EMPTY;
}

export function isProviderConnected(providerId: string): boolean {
  return getConnectedAccounts().providerIds.includes(providerId.toLowerCase());
}

export async function fetchConnectedAccounts(force = false): Promise<ConnectedAccountsData> {
  try {
    if (force) {
      return await queryClient.invalidate(CACHE_KEY, fetcher, { staleTime: STALE_TIME });
    }
    return await queryClient.fetch(CACHE_KEY, fetcher, { staleTime: STALE_TIME });
  } catch {
    return queryClient.read<ConnectedAccountsData>(CACHE_KEY) ?? EMPTY;
  }
}

export function subscribe(listener: () => void): () => void {
  return queryClient.subscribe(CACHE_KEY, listener);
}
