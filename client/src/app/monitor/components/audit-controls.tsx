'use client';

import { useEffect, useId, useState } from 'react';
import { ChevronLeft, ChevronRight, Search } from 'lucide-react';

/**
 * Fetcher for the audit endpoints that keeps the API's error message and status,
 * so a rejected filter (400, e.g. "Search can't look for secret values.") can be
 * shown to the user instead of a generic failure.
 */
export async function auditFetcher<T>(key: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(key, { credentials: 'include', signal });
  if (res.ok) return res.json() as Promise<T>;
  let message = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body?.error === 'string') message = body.error;
  } catch {
    // Not JSON: keep the generic message.
  }
  throw Object.assign(new Error(message), { status: res.status });
}

/** The constant message of a rejected filter (400), or null for any other outcome. */
export function filterErrorOf(error: Error | null): string | null {
  return error && (error as Error & { status?: number }).status === 400 ? error.message : null;
}

export function FilterError({ message }: { message: string | null }) {
  if (!message) return null;
  return <p role="alert" className="-mt-2 mb-3 text-xs text-amber-400">{message}</p>;
}

export const selectCls =
  'bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2 py-1 text-xs text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-600 max-w-[180px]';

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/**
 * Each filter change is a new query key, so `data` is empty until it loads.
 * Keep the previous response only while the next one is loading, so nothing
 * disappears or jumps; once a request fails, the old results are not shown as
 * if they were current. `refreshing` lets the caller dim them.
 */
export function useKeptData<T>(data: T | undefined, isLoading: boolean): { view: T | undefined; refreshing: boolean } {
  const [previous, setPrevious] = useState<T>();
  useEffect(() => { if (data) setPrevious(data); }, [data]);
  const refreshing = !data && !!previous && isLoading;
  return { view: data ?? (refreshing ? previous : undefined), refreshing };
}

/** Polite screen-reader announcement of a list's state after a filter change. */
export function LiveStatus({ text }: { text: string }) {
  return <p className="sr-only" role="status" aria-live="polite">{text}</p>;
}

export interface UserOption { value: string; label: string }

/**
 * User filter: pick a suggested user, or type any member's email (the list of
 * suggestions is capped, so typing reaches everyone). Applied on pick, Enter or blur.
 */
export function UserFilter({ users, value, onChange }: {
  users: UserOption[]; value: string; onChange: (v: string) => void;
}) {
  const [text, setText] = useState(value);
  useEffect(() => { setText(value); }, [value]);
  const listId = useId();
  const commit = (v: string) => { const t = v.trim(); if (t !== value) onChange(t); };
  return (
    <>
      <input
        list={listId}
        value={text}
        maxLength={255}
        placeholder="All users"
        aria-label="User: pick one, or type an email"
        title="Pick a user, or type any member's email"
        onChange={(e) => {
          const v = e.target.value;
          setText(v);
          // A suggestion picked from the list, or the box cleared: apply now.
          if (v === '' || users.some(u => u.value === v)) commit(v);
        }}
        onBlur={() => commit(text)}
        onKeyDown={(e) => { if (e.key === 'Enter') commit(text); }}
        className={`${selectCls} w-44`}
      />
      <datalist id={listId}>
        {users.map(u => <option key={u.value} value={u.value}>{u.label}</option>)}
      </datalist>
    </>
  );
}

/** Read a query-string value after mount (the page is prerendered, so not during render). */
export function useUrlParam(key: string, fallback: string): [string, (v: string) => void, boolean] {
  const [value, setValue] = useState(fallback);
  const [ready, setReady] = useState(false);  // false until the URL has been read
  useEffect(() => {
    const v = new URLSearchParams(window.location.search).get(key);
    if (v) setValue(v);
    setReady(true);
  }, [key]);
  const update = (v: string) => {
    setValue(v);
    const url = new URL(window.location.href);
    if (v === fallback) url.searchParams.delete(key);
    else url.searchParams.set(key, v);
    window.history.replaceState(window.history.state, '', url);
  };
  return [value, update, ready];
}

export function SearchBox({ value, onChange, placeholder, label }: {
  value: string; onChange: (v: string) => void; placeholder: string; label: string;
}) {
  return (
    <div className="relative flex-1 min-w-[200px]">
      <Search className="h-3.5 w-3.5 text-zinc-500 absolute left-2 top-1/2 -translate-y-1/2" />
      <input
        type="search"
        value={value}
        onChange={e => onChange(e.target.value)}
        maxLength={200}
        placeholder={placeholder}
        aria-label={label}
        className="w-full bg-zinc-800/50 border border-zinc-700/50 rounded-md pl-7 pr-2.5 py-1 text-xs text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-600"
      />
    </div>
  );
}

export function Pager({ total, page, totalPages, noun, onPage }: {
  total: number; page: number; totalPages: number; noun: [string, string]; onPage: (p: number) => void;
}) {
  const btn = 'p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed';
  return (
    <div className="flex items-center justify-between mt-4">
      <p className="text-xs text-zinc-500">
        {total.toLocaleString()} {total === 1 ? noun[0] : noun[1]} — page {page} of {totalPages}
      </p>
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1} aria-label="Previous page" className={btn}>
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button onClick={() => onPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages} aria-label="Next page" className={btn}>
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      )}
    </div>
  );
}
