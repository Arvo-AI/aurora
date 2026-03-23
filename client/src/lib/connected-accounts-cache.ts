type Listener = () => void;

interface CacheState {
  accounts: Record<string, any>;
  providerIds: string[];
  fetchedAt: number;
}

const DEBOUNCE_MS = 2_000;

let state: CacheState = { accounts: {}, providerIds: [], fetchedAt: 0 };
let inflight: Promise<CacheState> | null = null;
let listeners: Set<Listener> = new Set();
let initialized = false;

function notify() {
  listeners.forEach(fn => fn());
}

async function doFetch(): Promise<CacheState> {
  const response = await fetch('/api/connected-accounts', { credentials: 'include' });
  if (!response.ok) throw new Error(`connected-accounts ${response.status}`);
  const data = await response.json();
  const accounts = data.accounts || {};
  const providerIds = Object.keys(accounts).map(k => k.toLowerCase());
  state = { accounts, providerIds, fetchedAt: Date.now() };
  initialized = true;
  notify();
  return state;
}

export function getConnectedAccounts(): CacheState {
  return state;
}

export function isProviderConnected(providerId: string): boolean {
  return state.providerIds.includes(providerId.toLowerCase());
}

/**
 * Fetch connected accounts. Deduplicates concurrent calls and skips
 * if the cache was refreshed within DEBOUNCE_MS.
 */
export async function fetchConnectedAccounts(force = false): Promise<CacheState> {
  if (!force && initialized && Date.now() - state.fetchedAt < DEBOUNCE_MS) {
    return state;
  }
  if (inflight) return inflight;
  inflight = doFetch().finally(() => { inflight = null; });
  return inflight;
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

if (typeof window !== 'undefined') {
  const refresh = () => fetchConnectedAccounts(true);
  window.addEventListener('providerStateChanged', refresh);
  window.addEventListener('providerConnectionAction', refresh);
}
