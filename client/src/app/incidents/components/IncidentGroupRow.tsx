'use client';

import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { AlertTriangle, CheckCircle2, ChevronRight, Clock, Loader2, Repeat } from 'lucide-react';
import { Incident, incidentsService } from '@/lib/services/incidents';
import { IncidentGroup, isGroupStalled, isInvestigating, isStalled, lastFire } from '@/lib/incident-grouping';
import ExpandablePanel, { ExpandChevron } from './ExpandablePanel';

interface IncidentGroupRowProps {
  group: IncidentGroup;
  expanded: boolean;
  onToggle: () => void;
}

function formatFireTime(incident: Incident): string {
  const fired = new Date(lastFire(incident));
  const sameDay = fired.toDateString() === new Date().toDateString();
  return sameDay
    ? fired.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : fired.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function OccurrenceStatusIcon({ incident, now }: { incident: Incident; now: number }) {
  if (isStalled(incident, now)) {
    return <AlertTriangle className="h-3.5 w-3.5 text-red-400" aria-label="Investigation stalled" />;
  }
  if (isInvestigating(incident) || incident.status === 'investigating') {
    return <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-label="Investigating" />;
  }
  return <CheckCircle2 className="h-3.5 w-3.5 text-muted-foreground" aria-label="Analyzed" />;
}

/**
 * One card per recurrence group on /incidents. The title row links to the
 * anchor; the chevron expands the chronological occurrence list without
 * navigating.
 */
export default function IncidentGroupRow({ group, expanded, onToggle }: IncidentGroupRowProps) {
  const { anchor } = group;
  const now = Date.now();
  const isActive = group.section === 'investigating';
  const stalled = isGroupStalled(group, now);
  // Severity is typed as a closed union but the API can still send 'unknown'.
  const severity: string = anchor.alert.severity;
  const showSeverity = (severity && severity !== 'unknown') || anchor.status === 'analyzed';

  return (
    <Card className={`hover:border-primary/50 transition-colors ${isActive ? 'border-l-4 border-l-muted-foreground' : ''}`}>
      <CardContent className="p-0">
        {/* The link carries the row padding so the whole header (bar the chevron) navigates, like IncidentRow. */}
        <div className="flex items-center">
          <Link
            href={`/incidents/${anchor.id}`}
            aria-label={`View incident: ${anchor.alert.title}`}
            className="flex-1 min-w-0 flex items-center gap-4 py-3 pl-4 pr-2"
          >
            {showSeverity && (
              <Badge className={incidentsService.getSeverityColor(anchor.alert.severity)}>
                {anchor.alert.severity} severity
              </Badge>
            )}

            <div className="flex-1 min-w-0">
              <p className="font-medium truncate">{anchor.alert.title}</p>
              <div className="flex items-center gap-3 text-sm text-muted-foreground mt-0.5">
                {anchor.alert.service !== 'unknown' && <span>{anchor.alert.service}</span>}
                <span className="flex items-center gap-1">
                  <Repeat className="h-3 w-3" />
                  fired {group.occurrenceCount} times
                </span>
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {incidentsService.formatDuration(new Date(group.lastFiredAt).toISOString())}
                </span>
                {stalled && (
                  <span className="flex items-center gap-1">
                    <AlertTriangle className="h-3 w-3 text-red-400" /> Investigation stalled
                  </span>
                )}
                {!stalled && (isActive || group.investigating) && (
                  <span className="flex items-center gap-1">
                    <Loader2 className="h-3 w-3 animate-spin" /> Aurora investigating
                  </span>
                )}
              </div>
            </div>
          </Link>

          <button
            type="button"
            aria-expanded={expanded}
            aria-label={expanded ? 'Hide occurrences' : 'Show occurrences'}
            onClick={onToggle}
            className={`mr-3 p-1 rounded transition-colors ${expanded ? 'bg-muted' : 'hover:bg-muted'}`}
          >
            <ExpandChevron open={expanded} className="text-muted-foreground" />
          </button>
        </div>

        <ExpandablePanel open={expanded} className="mx-4" contentClassName="py-3 border-t border-border space-y-1">
          {group.occurrences.map((occurrence, index) => (
            <Link
              key={occurrence.id}
              href={`/incidents/${occurrence.id}`}
              className="flex items-center gap-3 px-2 py-1.5 rounded-md text-sm hover:bg-muted/60 transition-colors"
            >
              <span className="w-32 shrink-0 tabular-nums text-muted-foreground whitespace-nowrap">{formatFireTime(occurrence)}</span>
              <span className="flex-1 min-w-0 truncate">
                occurrence {index + 1}
                {index === 0 && <span className="text-muted-foreground"> · first</span>}
              </span>
              <OccurrenceStatusIcon incident={occurrence} now={now} />
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            </Link>
          ))}
        </ExpandablePanel>
      </CardContent>
    </Card>
  );
}
