/**
 * Aurora Query Client
 *
 * Single source of truth for all data fetching. Two layers:
 *
 *   1. queryClient  — imperative cache + fetch with dedup, retry, stale-while-revalidate
 *   2. useQuery     — React hook that subscribes to queryClient
 *   3. fetchR       — low-level fetch() replacement with retry + timeout (for mutations)
 *
 * Every GET in the app goes through queryClient (deduped, cached).
 * Every POST/PUT/DELETE goes through fetchR (retried, timed out).
 * There is no third path.
 */

import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';

// ─── Types ──────────────────────────────────────────────────────────

export type Fetcher<T> = (key: string, signal: AbortSignal) => Promise<T>;

export interface QueryOptions<T = unknown> {
  staleTime?: number;        // ms before data is stale (default 30_000)
  retryCount?: number;       // max retries on failure (default 3)
  retryDelay?: number;       // base delay for exponential backoff (default 2_000)
  timeout?: number;          // abort after ms (default 20_000)
  revalidateOnFocus?: boolean;
  revalidateOnEvents?: string[];
  enabled?: boolean;
  onSuccess?: (data: T) => void;
  onError?: (err: Error) => void;
}

// ─── Internal cache ─────────────────────────────────────────────────

interface Entry<T = unknown> {
  data: T | undefined;
  error: Error | null;
  fetchedAt: number;
  validating: boolean;
  inflight: Promise<T> | null;
  retryTimer: ReturnType<typeof setTimeout> | null;
  listeners: Set<() => void>;
  version: number;
}

const store = new Map<string, Entry>();
const snapshots = new Map<string, { version: number; snap: Snap }>();

interface Snap { data: unknown; error: Error | null; validating: boolean; version: number }

function entry<T>(key: string): Entry<T> {
  if (!store.has(key)) {
    store.set(key, {
      data: undefined, error: null, fetchedAt: 0, validating: false,
      inflight: null, retryTimer: null, listeners: new Set(), version: 0,
    });
  }
  return store.get(key) as Entry<T>;
}

function emit(e: Entry) {
  e.version++;
  e.listeners.forEach(fn => fn());
}

function snap<T>(key: string): Snap | undefined {
  const e = store.get(key);
  if (!e) return undefined;
  const cached = snapshots.get(key);
  if (cached && cached.version === e.version) return cached.snap;
  const s: Snap = { data: e.data, error: e.error, validating: e.validating, version: e.version };
  snapshots.set(key, { version: e.version, snap: s });
  return s;
}

// ─── Core revalidation engine ───────────────────────────────────────

const DEFAULTS = { retryCount: 3, retryDelay: 2_000, timeout: 20_000 };

interface RevalidateOpts {
  retryCount: number;
  retryDelay: number;
  timeout: number;
  onSuccess: (data: unknown) => void;
  onError: (err: Error) => void;
}

async function doFetch<T>(
  key: string, fetcher: Fetcher<T>, opts: RevalidateOpts, attempt = 0,
): Promise<T> {
  const e = entry<T>(key);
  if (e.inflight && attempt === 0) return e.inflight;

  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), opts.timeout);

  const p = (async (): Promise<T> => {
    e.validating = true;
    emit(e);
    try {
      const data = await fetcher(key, ac.signal);
      clearTimeout(t);
      e.data = data; e.error = null; e.fetchedAt = Date.now();
      e.validating = false; e.inflight = null;
      emit(e);
      opts.onSuccess(data);
      return data;
    } catch (err) {
      clearTimeout(t);
      const error = err instanceof Error ? err : new Error(String(err));
      if (error.name === 'AbortError') {
        e.validating = false; e.inflight = null; emit(e);
        throw error;
      }
      if (attempt < opts.retryCount) {
        e.inflight = null;
        const delay = opts.retryDelay * Math.pow(2, attempt);
        return new Promise<T>((resolve, reject) => {
          e.retryTimer = setTimeout(() => {
            e.retryTimer = null;
            doFetch(key, fetcher, opts, attempt + 1).then(resolve, reject);
          }, delay);
        });
      }
      e.error = error; e.fetchedAt = Date.now();
      e.validating = false; e.inflight = null;
      emit(e);
      opts.onError(error);
      throw error;
    }
  })();

  if (attempt === 0) e.inflight = p;
  return p;
}

function resolveOpts<T>(o?: Partial<QueryOptions<T>>): RevalidateOpts {
  return {
    retryCount: o?.retryCount ?? DEFAULTS.retryCount,
    retryDelay: o?.retryDelay ?? DEFAULTS.retryDelay,
    timeout: o?.timeout ?? DEFAULTS.timeout,
    onSuccess: (o?.onSuccess ?? (() => {})) as (data: unknown) => void,
    onError: o?.onError ?? (() => {}),
  };
}

// ─── queryClient — the single imperative API ────────────────────────

export const queryClient = {
  /**
   * Fetch data with dedup + stale check.
   * If data is fresh, returns cache. If inflight, piggybacks. Otherwise fetches.
   */
  fetch<T>(key: string, fetcher: Fetcher<T>, opts?: Partial<QueryOptions<T>>): Promise<T> {
    const e = entry<T>(key);
    const staleTime = opts?.staleTime ?? 30_000;
    if (e.inflight) return e.inflight;
    if (e.data !== undefined && Date.now() - e.fetchedAt < staleTime) {
      return Promise.resolve(e.data as T);
    }
    return doFetch(key, fetcher, resolveOpts(opts));
  },

  /** Force-refresh, cancelling any in-flight request. Use after mutations. */
  invalidate<T>(key: string, fetcher: Fetcher<T>, opts?: Partial<QueryOptions<T>>): Promise<T> {
    const e = entry<T>(key);
    if (e.retryTimer) { clearTimeout(e.retryTimer); e.retryTimer = null; }
    e.inflight = null;
    return doFetch(key, fetcher, resolveOpts(opts));
  },

  /** Sync read from cache. Never triggers a fetch. */
  read<T>(key: string): T | undefined {
    return store.get(key)?.data as T | undefined;
  },

  /** Optimistic write — sets data + notifies all subscribers. */
  set<T>(key: string, data: T): void {
    const e = entry<T>(key);
    e.data = data; e.fetchedAt = Date.now(); e.error = null;
    emit(e);
  },

  /** Subscribe to cache changes. Returns unsubscribe function. */
  subscribe(key: string, listener: () => void): () => void {
    const e = entry(key);
    e.listeners.add(listener);
    return () => { e.listeners.delete(listener); };
  },
};

// ─── useQuery — React hook ──────────────────────────────────────────

export interface QueryResult<T> {
  data: T | undefined;
  error: Error | null;
  isLoading: boolean;
  isValidating: boolean;
  mutate: () => Promise<T | void>;
}

export function useQuery<T>(
  key: string | null,
  fetcher: Fetcher<T>,
  options?: QueryOptions<T>,
): QueryResult<T> {
  const opts = {
    staleTime: options?.staleTime ?? 30_000,
    retryCount: options?.retryCount ?? DEFAULTS.retryCount,
    retryDelay: options?.retryDelay ?? DEFAULTS.retryDelay,
    revalidateOnFocus: options?.revalidateOnFocus ?? true,
    revalidateOnEvents: options?.revalidateOnEvents ?? [],
    enabled: options?.enabled ?? true,
    timeout: options?.timeout ?? DEFAULTS.timeout,
    onSuccess: options?.onSuccess ?? (() => {}),
    onError: options?.onError ?? (() => {}),
  };

  const optsRef = useRef(opts);
  optsRef.current = opts;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const active = key && opts.enabled ? key : null;

  const sub = useCallback(
    (cb: () => void) => active ? queryClient.subscribe(active, cb) : () => {},
    [active],
  );
  const getSnap = useCallback(
    () => active ? snap<T>(active) : undefined,
    [active],
  );
  const s = useSyncExternalStore(sub, getSnap, () => undefined);

  const mutate = useCallback(async () => {
    if (!active) return;
    try { return await queryClient.invalidate(active, fetcherRef.current, optsRef.current); }
    catch { /* error lands in cache */ }
  }, [active]);

  useEffect(() => {
    if (!active) return;
    queryClient.fetch(active, fetcherRef.current, optsRef.current).catch(() => {});
  }, [active]);

  useEffect(() => {
    if (!active || !optsRef.current.revalidateOnFocus) return;
    const h = () => {
      if (document.visibilityState !== 'visible') return;
      queryClient.fetch(active, fetcherRef.current, optsRef.current).catch(() => {});
    };
    document.addEventListener('visibilitychange', h);
    return () => document.removeEventListener('visibilitychange', h);
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const evts = optsRef.current.revalidateOnEvents;
    if (!evts.length) return;
    const h = () => { queryClient.fetch(active, fetcherRef.current, optsRef.current).catch(() => {}); };
    evts.forEach(e => window.addEventListener(e, h));
    return () => { evts.forEach(e => window.removeEventListener(e, h)); };
  }, [active, opts.revalidateOnEvents.join(',')]);

  useEffect(() => () => {
    if (!active) return;
    const e = store.get(active);
    if (e?.retryTimer) { clearTimeout(e.retryTimer); e.retryTimer = null; }
  }, [active]);

  return {
    data: (s?.data as T) ?? undefined,
    error: s?.error ?? null,
    isLoading: active !== null && (!s || (s.data === undefined && !s.error)),
    isValidating: s?.validating ?? false,
    mutate,
  };
}

// ─── fetchR — low-level fetch with retry + timeout (for mutations) ──

export async function fetchR(
  input: RequestInfo | URL,
  init?: RequestInit & { retries?: number; retryDelay?: number; timeout?: number },
): Promise<Response> {
  const { retries = 2, retryDelay = 1500, timeout = 20_000, ...fi } = init ?? {};
  for (let i = 0; i <= retries; i++) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), timeout);
    if (fi.signal) fi.signal.addEventListener('abort', () => ac.abort());
    try {
      const res = await fetch(input, { ...fi, signal: ac.signal });
      clearTimeout(t);
      if (!res.ok && i < retries && res.status >= 500) {
        await new Promise(r => setTimeout(r, retryDelay * (i + 1)));
        continue;
      }
      return res;
    } catch (err) {
      clearTimeout(t);
      if (i < retries && err instanceof Error && err.name !== 'AbortError') {
        await new Promise(r => setTimeout(r, retryDelay * (i + 1)));
        continue;
      }
      throw err;
    }
  }
  return fetch(input, fi);
}

// ─── jsonFetcher — default fetcher for useQuery ─────────────────────

export function jsonFetcher<T = unknown>(key: string, signal: AbortSignal): Promise<T> {
  return fetch(key, { credentials: 'include', signal }).then(r => {
    if (!r.ok) throw new Error(`${key} ${r.status} ${r.statusText}`);
    return r.json() as Promise<T>;
  });
}
