'use client';

import { useEffect, useMemo, useRef } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Zap, Clock, ChevronRight, Loader2, CheckCircle2, Link2, GitMerge } from 'lucide-react';
import { Incident, incidentsService } from '@/lib/services/incidents';
import { useConnectedAccounts } from '@/hooks/useConnectedAccounts';
import { connectorRegistry } from '@/components/connectors/ConnectorRegistry';
import { useQuery } from '@/lib/query';

const ALERT_CATEGORIES = new Set(['Monitoring', 'Incident Management']);

const ALERT_PROVIDERS = new Set([
  'grafana', 'datadog', 'netdata', 'splunk', 'dynatrace',
  'coroot', 'newrelic', 'thousandeyes', 'pagerduty', 'bigpanda',
]);

interface IncidentsResponse { incidents: any[] }

const incidentsFetcher = async (key: string, signal: AbortSignal) => {
  const res = await fetch(key, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`incidents ${res.status}`);
  const data: IncidentsResponse = await res.json();
  return (data.incidents || []).map((inc: any): Incident => ({
    id: inc.id,
    alert: {
      source: inc.sourceType,
      sourceUrl: inc.alert?.sourceUrl || '',
      rawPayload: '',
      triggeredAt: inc.startedAt,
      title: inc.alert.title,
      severity: inc.severity,
      service: inc.alert.service,
    },
    status: inc.status,
    auroraStatus: inc.auroraStatus || 'idle',
    summary: inc.summary || '',
    streamingThoughts: inc.streamingThoughts || [],
    suggestions: inc.suggestions || [],
    correlatedAlertCount: inc.correlatedAlertCount || 0,
    mergedIntoIncidentId: inc.mergedIntoIncidentId,
    mergedIntoTitle: inc.mergedIntoTitle,
    postMortem: inc.postMortem ?? undefined,
    startedAt: inc.startedAt,
    analyzedAt: inc.analyzedAt,
    createdAt: inc.createdAt,
    updatedAt: inc.updatedAt,
    activeTab: inc.activeTab || 'thoughts',
  }));
};

export default function IncidentsPage() {
  const { providerIds } = useConnectedAccounts();

  const isConnectedToIncidentPlatform = useMemo(
    () => providerIds.some(id => ALERT_PROVIDERS.has(id)),
    [providerIds],
  );

  const { data: incidents = [], isLoading, mutate } = useQuery<Incident[]>(
    '/api/incidents',
    incidentsFetcher,
    { staleTime: 10_000, revalidateOnFocus: true },
  );

  const mutateRef = useRef(mutate);
  mutateRef.current = mutate;

  // Real-time updates via SSE — no polling, no manual refresh needed
  useEffect(() => {
    const eventSource = new EventSource('/api/incidents/stream');
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'incident_update') {
          mutateRef.current();
        }
      } catch { /* ignore malformed messages */ }
    };
    eventSource.onerror = () => { /* EventSource reconnects automatically */ };
    return () => eventSource.close();
  }, []);

  const activeIncidents = useMemo(() => incidents.filter(i => i.status === 'investigating'), [incidents]);
  const analyzedIncidents = useMemo(() => incidents.filter(i => i.status === 'analyzed' || i.status === 'resolved'), [incidents]);
  const mergedIncidents = useMemo(() => incidents.filter(i => i.status === 'merged'), [incidents]);

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <div className="mb-8">
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <Zap className="h-6 w-6 text-foreground" />
          Incidents
        </h1>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <div className="space-y-8">
          {activeIncidents.length > 0 && (
            <div>
              <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-muted-foreground opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-muted-foreground"></span>
                </span>
                Investigating ({activeIncidents.length})
              </h2>
              <div className="space-y-2">
                {activeIncidents.map(incident => (
                  <IncidentRow key={incident.id} incident={incident} />
                ))}
              </div>
            </div>
          )}

          {analyzedIncidents.length > 0 && (
            <div>
              <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide mb-3 flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
                Analyzed
              </h2>
              <div className="space-y-2">
                {analyzedIncidents.map(incident => (
                  <IncidentRow key={incident.id} incident={incident} />
                ))}
              </div>
            </div>
          )}

          {mergedIncidents.length > 0 && (
            <div>
              <h2 className="text-sm font-medium text-zinc-600 uppercase tracking-wide mb-3 flex items-center gap-2">
                <GitMerge className="h-4 w-4 text-zinc-600" />
                Merged
              </h2>
              <div className="space-y-2">
                {mergedIncidents.map(incident => (
                  <IncidentRow key={incident.id} incident={incident} />
                ))}
              </div>
            </div>
          )}

          {incidents.length === 0 && (
            <Card>
              <CardContent className="py-12 text-center">
                {!isConnectedToIncidentPlatform ? (
                  <ConnectPlatformCTA />
                ) : (
                  <>
                    <CheckCircle2 className="h-10 w-10 mx-auto text-green-500 mb-3" />
                    <p className="font-medium">All clear</p>
                    <p className="text-sm text-muted-foreground">No incidents yet</p>
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

function IncidentRow({ incident }: { incident: Incident }) {
  const isActive = incident.status === 'investigating';
  const isMerged = incident.status === 'merged';
  const showSeverity = (incident.alert.severity && incident.alert.severity !== 'unknown') || incident.status === 'analyzed';
  const correlatedCount = incident.correlatedAlertCount || 0;

  return (
    <Link href={`/incidents/${incident.id}`} aria-label={`View incident: ${incident.alert.title}`}>
      <Card className={`hover:border-primary/50 transition-colors cursor-pointer ${isActive ? 'border-l-4 border-l-muted-foreground' : ''} ${isMerged ? 'opacity-60' : ''}`}>
        <CardContent className="py-3 px-4">
          <div className="flex items-center gap-4">
            {/* Severity - hide if unknown during investigation */}
            {showSeverity && (
              <Badge className={incidentsService.getSeverityColor(incident.alert.severity)}>
                {incident.alert.severity} severity
              </Badge>
            )}

            {/* Title & Service */}
            <div className="flex-1 min-w-0">
              <p className={`font-medium truncate ${isMerged ? 'text-zinc-500' : ''}`}>{incident.alert.title}</p>
              <div className="flex items-center gap-3 text-sm text-muted-foreground mt-0.5">
                {incident.alert.service !== 'unknown' && <span>{incident.alert.service}</span>}
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {incidentsService.formatDuration(incident.startedAt)}
                </span>
                {correlatedCount > 0 && (
                  <span className="flex items-center gap-1 text-muted-foreground">
                    <Link2 className="h-3 w-3" />
                    {correlatedCount} related
                  </span>
                )}
                {isActive && (
                  <span className="flex items-center gap-1 text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Aurora investigating
                  </span>
                )}
                {isMerged && (
                  <span className="flex items-center gap-1 text-zinc-500">
                    <GitMerge className="h-3 w-3" />
                    {incident.mergedIntoTitle 
                      ? `Merged into "${incident.mergedIntoTitle}"`
                      : 'Merged into another incident'
                    }
                  </span>
                )}
              </div>
            </div>

            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

const alertConnectors = connectorRegistry
  .getAll()
  .filter(c => c.category && ALERT_CATEGORIES.has(c.category) && c.path);

function ConnectPlatformCTA() {
  return (
    <div>
      <p className="font-medium mb-1">Connect a monitoring platform</p>
      <p className="text-sm text-muted-foreground mb-6">
        Pick one to start receiving alerts
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 max-w-lg mx-auto">
        {alertConnectors.map(c => (
          <Link
            key={c.id}
            href={c.path!}
            className="flex flex-col items-center gap-2 p-3 rounded-lg border border-border hover:border-primary/50 hover:bg-muted/50 transition-colors"
          >
            {c.iconPath ? (
              <div className={`p-1.5 rounded-md ${c.iconBgColor || 'bg-muted'}`}>
                <img src={c.iconPath} alt="" className="h-6 w-6 object-contain" />
              </div>
            ) : c.icon ? (
              <div className={`p-1.5 rounded-md ${c.iconBgColor || 'bg-muted'}`}>
                <c.icon className={`h-6 w-6 ${c.iconColor || 'text-foreground'}`} />
              </div>
            ) : null}
            <span className="text-xs font-medium">{c.name}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
