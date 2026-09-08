'use client';

import { useState } from 'react';
import Link from 'next/link';
import { ChevronRight, Repeat } from 'lucide-react';
import { IncidentOccurrence, incidentsService } from '@/lib/services/incidents';
import ExpandablePanel, { ExpandChevron } from './ExpandablePanel';

interface OccurrencesSectionProps {
  /** Recurrences folded into this anchor (newest N); the anchor itself is occurrence 1. */
  occurrences: IncidentOccurrence[];
  /** Full member count from the server; larger than occurrences.length when the list was capped. */
  total: number;
}

export default function OccurrencesSection({ occurrences, total }: Readonly<OccurrencesSectionProps>) {
  const [isExpanded, setIsExpanded] = useState(false);
  const notShown = total - occurrences.length;
  const fired = 1 + total;

  return (
    <div className="mt-4">
      <button
        type="button"
        aria-expanded={isExpanded}
        onClick={() => setIsExpanded(v => !v)}
        className={`w-full flex items-center justify-between px-4 py-3 rounded-lg border bg-zinc-900/50 border-zinc-800 hover:bg-zinc-800/50 hover:border-zinc-700 transition-all duration-200 ${
          isExpanded ? 'rounded-b-none border-b-0' : ''
        }`}
      >
        <div className="flex items-center gap-3">
          <div className="p-1.5 rounded-md bg-zinc-800">
            <Repeat className="w-4 h-4 text-zinc-400" />
          </div>
          <div className="text-left">
            <span className="text-sm font-medium text-zinc-300">Fired {fired} times</span>
            <p className="text-xs text-zinc-500 mt-0.5">Later occurrences of this root cause</p>
          </div>
        </div>
        <div className={`p-1 rounded transition-all duration-200 ${isExpanded ? 'bg-zinc-700' : 'hover:bg-zinc-800'}`}>
          <ExpandChevron open={isExpanded} className="text-zinc-400" />
        </div>
      </button>

      <ExpandablePanel
        open={isExpanded}
        className={`border-zinc-800 ${isExpanded ? 'border border-t-0 rounded-b-lg' : ''}`}
        contentClassName="p-3 space-y-1 bg-zinc-900/20"
      >
        {/* The list is oldest-first and the server cut the oldest members, so the gap precedes the first row. */}
        {notShown > 0 && (
          <p className="px-3 py-2 text-xs text-zinc-500">
            {notShown} older {notShown === 1 ? 'occurrence' : 'occurrences'} not shown
          </p>
        )}
        {occurrences.map(occurrence => (
          <Link
            key={occurrence.id}
            href={`/incidents/${occurrence.id}`}
            className="flex items-center gap-3 px-3 py-2 rounded-md text-sm hover:bg-zinc-800/50 transition-colors"
          >
            <span className="w-20 shrink-0 text-xs text-zinc-500">
              {incidentsService.formatTimeAgo(occurrence.alertFiredAt ?? occurrence.startedAt)}
            </span>
            <span className="flex-1 min-w-0 truncate text-zinc-200">{occurrence.alertTitle}</span>
            <span className="text-xs text-zinc-500 capitalize">{occurrence.status}</span>
            <ChevronRight className="w-4 h-4 text-zinc-500" />
          </Link>
        ))}
      </ExpandablePanel>
    </div>
  );
}
