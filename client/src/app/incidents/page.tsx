'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Zap, Clock, ChevronRight, Loader2, CheckCircle2, AlertTriangle, Link2, GitMerge } from 'lucide-react';
import { 
  Incident, 
  incidentsService 
} from '@/lib/services/incidents';
import { grafanaService } from '@/lib/services/grafana';
import { pagerdutyService } from '@/lib/services/pagerduty';
import { netdataService } from '@/lib/services/netdata';
import { splunkService } from '@/lib/services/splunk';
import { datadogService } from '@/lib/services/datadog';
import { useRouter } from 'next/navigation';

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [loading, setLoading] = useState(true);
  const [isConnectedToIncidentPlatform, setIsConnectedToIncidentPlatform] = useState<boolean | null>(null);
  const router = useRouter();

  const refreshIncidents = async (silent: boolean = false) => {
    try {
      if (!silent) {
        setLoading(true);
      }

      const [data, grafanaStatus, pagerdutyStatus, netdataStatus, splunkStatus, datadogStatus] = await Promise.all([
        incidentsService.getIncidents(),
        // Only check connection status on initial load (not silent refreshes)
        silent ? Promise.resolve(null) : grafanaService.getStatus(),
        silent ? Promise.resolve(null) : pagerdutyService.getStatus(),
        silent ? Promise.resolve(null) : netdataService.getStatus(),
        silent ? Promise.resolve(null) : splunkService.getStatus(),
        silent ? Promise.resolve(null) : datadogService.getStatus()
      ]);

      // Update connection status if this is initial load
      // Consider connected if Grafana, PagerDuty, Netdata, Splunk, or Datadog is connected
      if (!silent) {
        const grafanaConnected = grafanaStatus?.connected ?? false;
        const pagerdutyConnected = pagerdutyStatus?.connected ?? false;
        const netdataConnected = netdataStatus?.connected ?? false;
        const splunkConnected = splunkStatus?.connected ?? false;
        const datadogConnected = datadogStatus?.connected ?? false;
        setIsConnectedToIncidentPlatform(grafanaConnected || pagerdutyConnected || netdataConnected || splunkConnected || datadogConnected);
      }

      // Only update state if the list actually changed (prevents unnecessary re-renders)
      setIncidents(prev => {
        if (prev.length !== data.length) return data;

        const prevMap = new Map(prev.map(i => [i.id, i]));

        const hasChanges = data.some(next => {
          const prevIncident = prevMap.get(next.id);
          return (
            !prevIncident ||
            prevIncident.status !== next.status ||
            prevIncident.auroraStatus !== next.auroraStatus ||
            prevIncident.summary !== next.summary ||
            prevIncident.analyzedAt !== next.analyzedAt ||
            prevIncident.updatedAt !== next.updatedAt
          );
        });

        return hasChanges ? data : prev;
      });
    } catch (error) {
      console.error('Failed to refresh incidents:', error instanceof Error ? error.message : 'Unknown error');
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    let mounted = true;
    
    const load = async () => {
      if (mounted) {
        await refreshIncidents(false);
      }
    };
    
    load();
    
    return () => {
      mounted = false;
    };
  }, []);

  // Real-time updates via Server-Sent Events
  const refreshIncidentsRef = useRef(refreshIncidents);
  refreshIncidentsRef.current = refreshIncidents;

  useEffect(() => {
    // Connect to SSE endpoint for real-time incident updates
    const eventSource = new EventSource('/api/incidents/stream');
    
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        // Refresh incidents when we get an update
        if (data.type === 'incident_update') {
          console.log('[Incidents] Received real-time update:', data);
          refreshIncidentsRef.current(true); // Silent refresh
        }
      } catch (error) {
        console.error('[Incidents] Failed to parse SSE message:', error);
      }
    };
    
    eventSource.onerror = (error) => {
      console.error('[Incidents] SSE connection error:', error);
      // EventSource will automatically reconnect
    };
    
    return () => {
      eventSource.close();
    };
  }, []);

  // Refresh on window focus to catch new incidents from background (silent)
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        refreshIncidentsRef.current(true);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  const activeIncidents = incidents.filter(i => i.status === 'investigating');
  const analyzedIncidents = incidents.filter(i => i.status === 'analyzed');
  const mergedIncidents = incidents.filter(i => i.status === 'merged');

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      <div className="mb-8">
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <Zap className="h-6 w-6 text-foreground" />
          Incidents
        </h1>
      </div>

      {loading ? (
        <div className="flex justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <div className="space-y-8">
          {/* Active/Investigating Incidents */}
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

          {/* Analyzed */}
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

          {/* Merged */}
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
                {isConnectedToIncidentPlatform === false ? (
                  <>
                    <AlertTriangle className="h-10 w-10 mx-auto text-orange-500 mb-3" />
                    <p className="font-medium mb-2">No incident platform connected</p>
                    <p className="text-sm text-muted-foreground mb-4">
                      Connect to an incident platform to start receiving and analyzing alerts
                    </p>
                    <Button 
                      onClick={() => router.push('/connectors')}
                      className="mx-auto"
                    >
                      View Connectors
                    </Button>
                  </>
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
