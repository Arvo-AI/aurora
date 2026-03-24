type Listener = () => void;

interface CacheState {
  accounts: Record<string, any>;
  providerIds: string[];
  fetchedAt: number;
}

const DEBOUNCE_MS = 2_000;

let state: CacheState = { accounts: {}, providerIds: [], fetchedAt: 0 };
let inflight: Promise<CacheState> | null = null;
let pendingForceRefresh = false;
let listeners: Set<Listener> = new Set();
let initialized = false;

function notify() {
  listeners.forEach(fn => fn());
}

async function doFetch(): Promise<CacheState> {
  try {
    const response = await fetch('/api/connected-accounts', { credentials: 'include' });
    if (!response.ok) throw new Error(`connected-accounts ${response.status}`);
    const data = await response.json();
    const accounts = data.accounts || {};
    const providerIds = Object.keys(accounts).map(k => k.toLowerCase());
    state = { accounts, providerIds, fetchedAt: Date.now() };
    initialized = true;
    notify();
    return state;
  } catch (err) {
    state = { ...state, fetchedAt: Date.now() };
    initialized = true;
    notify();
    throw err;
  }
}

export function getConnectedAccounts(): CacheState {
  return state;
}

export function isProviderConnected(providerId: string): boolean {
  return state.providerIds.includes(providerId.toLowerCase());
}

/**
 * Fetch connected accounts. Deduplicates concurrent calls and skips
 * if the cache was refreshed within DEBOUNCE_MS. When force=true and
 * a request is already in flight, schedules a follow-up fetch after
 * the current one completes so state changes aren't lost.
 */
export async function fetchConnectedAccounts(force = false): Promise<CacheState> {
  if (!force && initialized && Date.now() - state.fetchedAt < DEBOUNCE_MS) {
    return state;
  }
  if (inflight) {
    if (force) {
      pendingForceRefresh = true;
    }
    return inflight;
  }
  inflight = doFetch()
    .catch(() => state)
    .finally(() => {
      inflight = null;
      if (pendingForceRefresh) {
        pendingForceRefresh = false;
        fetchConnectedAccounts(true);
      }
    });
  return inflight;
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

declare global {
  interface Window {
    __connectedAccountsRefresh?: () => void;
    __connectedAccountsCleanup?: () => void;
  }
}

if (typeof window !== 'undefined') {
  if (window.__connectedAccountsCleanup) {
    window.__connectedAccountsCleanup();
  }

  const refresh = () => {
    fetchConnectedAccounts(true).catch((err) => {
      console.error('[connected-accounts-cache] refresh failed:', err);
    });
  };

  window.__connectedAccountsRefresh = refresh;
  window.addEventListener('providerStateChanged', refresh);
  window.addEventListener('providerConnectionAction', refresh);

  window.__connectedAccountsCleanup = () => {
    window.removeEventListener('providerStateChanged', refresh);
    window.removeEventListener('providerConnectionAction', refresh);
  };
}
