'use client';

import { useState, useCallback } from 'react';
import { Shield, ChevronLeft, ChevronRight, Filter, User, Clock } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ChartPanel, EmptyState, type Period } from './charts';

interface AuditEvent {
  id: number;
  org_id: string;
  user_id: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  detail: Record<string, unknown>;
  ip_address: string | null;
  created_at: string;
}

interface AuditResponse {
  events: AuditEvent[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

const ACTION_COLORS: Record<string, string> = {
  login: 'bg-blue-500/10 text-blue-400',
  logout: 'bg-zinc-500/10 text-zinc-400',
  create: 'bg-emerald-500/10 text-emerald-400',
  update: 'bg-amber-500/10 text-amber-400',
  delete: 'bg-red-500/10 text-red-400',
  connect: 'bg-cyan-500/10 text-cyan-400',
  disconnect: 'bg-orange-500/10 text-orange-400',
  invoke: 'bg-violet-500/10 text-violet-400',
};

function actionBadge(action: string) {
  const base = action.split('_')[0] || action;
  const cls = ACTION_COLORS[base] || 'bg-zinc-500/10 text-zinc-400';
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {action}
    </span>
  );
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

export default function AuditTab({ period }: { period: Period }) {
  const [page, setPage] = useState(1);
  const [actionFilter, setActionFilter] = useState('');
  const [resourceFilter, setResourceFilter] = useState('');

  const params = new URLSearchParams({ period, page: String(page), per_page: '30' });
  if (actionFilter) params.set('action', actionFilter);
  if (resourceFilter) params.set('resource_type', resourceFilter);

  const { data, isLoading } = useQuery<AuditResponse>(
    `/api/audit-log?${params.toString()}`,
    jsonFetcher,
    { staleTime: 15_000 },
  );

  const goPage = useCallback((p: number) => {
    setPage(Math.max(1, p));
  }, []);

  return (
    <div className="space-y-6">
      <ChartPanel title="Audit Log" subtitle="User actions, permission changes, and system events" loading={isLoading}>
        {/* Filters */}
        <div className="flex items-center gap-3 mb-4">
          <div className="flex items-center gap-1.5">
            <Filter className="h-3.5 w-3.5 text-zinc-500" />
            <input
              type="text"
              placeholder="Filter by action..."
              value={actionFilter}
              onChange={(e) => { setActionFilter(e.target.value); setPage(1); }}
              className="bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2.5 py-1 text-xs text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-600 w-36 transition-all"
            />
          </div>
          <input
            type="text"
            placeholder="Filter by resource..."
            value={resourceFilter}
            onChange={(e) => { setResourceFilter(e.target.value); setPage(1); }}
            className="bg-zinc-800/50 border border-zinc-700/50 rounded-md px-2.5 py-1 text-xs text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-600 w-36 transition-all"
          />
          {(actionFilter || resourceFilter) && (
            <button
              onClick={() => { setActionFilter(''); setResourceFilter(''); setPage(1); }}
              className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
            >
              Clear
            </button>
          )}
        </div>

        {!data || data.events.length === 0 ? (
          <EmptyState
            icon={Shield}
            message="No audit events recorded"
            hint="Audit events will appear as users interact with Aurora — logins, config changes, agent invocations"
          />
        ) : (
          <>
            <div className="overflow-hidden rounded-lg border border-zinc-800/60">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800/60 text-zinc-500 text-xs uppercase tracking-wider">
                    <th className="text-left px-4 py-2.5 font-medium">Time</th>
                    <th className="text-left px-4 py-2.5 font-medium">User</th>
                    <th className="text-left px-4 py-2.5 font-medium">Action</th>
                    <th className="text-left px-4 py-2.5 font-medium">Resource</th>
                    <th className="text-left px-4 py-2.5 font-medium">Resource ID</th>
                    <th className="text-left px-4 py-2.5 font-medium">IP</th>
                  </tr>
                </thead>
                <tbody>
                  {data.events.map(evt => (
                    <tr key={evt.id} className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors duration-150">
                      <td className="px-4 py-2.5 text-zinc-500 text-xs whitespace-nowrap" style={{ fontVariantNumeric: 'tabular-nums' }}>
                        <div className="flex items-center gap-1.5">
                          <Clock className="h-3 w-3" />
                          {formatTimestamp(evt.created_at)}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <User className="h-3 w-3 text-zinc-600" />
                          <span className="text-zinc-300 text-xs font-medium truncate max-w-[120px]">{evt.user_id}</span>
                        </div>
                      </td>
                      <td className="px-4 py-2.5">{actionBadge(evt.action)}</td>
                      <td className="px-4 py-2.5 text-zinc-400 text-xs">{evt.resource_type}</td>
                      <td className="px-4 py-2.5 text-zinc-500 text-xs font-mono truncate max-w-[140px]">{evt.resource_id || '—'}</td>
                      <td className="px-4 py-2.5 text-zinc-600 text-xs" style={{ fontVariantNumeric: 'tabular-nums' }}>{evt.ip_address || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {data.total_pages > 1 && (
              <div className="flex items-center justify-between mt-4">
                <p className="text-xs text-zinc-500">
                  {data.total} events — page {data.page} of {data.total_pages}
                </p>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => goPage(page - 1)}
                    disabled={page <= 1}
                    className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => goPage(page + 1)}
                    disabled={page >= data.total_pages}
                    className="p-1 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </ChartPanel>
    </div>
  );
}
