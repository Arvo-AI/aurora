"use client";

import { useState } from "react";
import { Loader2, Clock, AlertTriangle, Plug, ChevronDown, ChevronUp } from "lucide-react";
import { useQuery, jsonFetcher } from "@/lib/query";

interface ActivityEvent {
  type: string;
  timestamp: string | null;
  description: string;
  // member_joined
  name?: string;
  email?: string;
  role?: string;
  userId?: string;
  // incident_created
  incidentId?: number;
  source?: string;
  title?: string;
  severity?: string;
  status?: string;
  // connector_added
  provider?: string;
  [key: string]: unknown;
}

const PROVIDER_ICONS: Record<string, string> = {
  aws: "/aws.ico",
  gcp: "/google-cloud-svgrepo-com.svg",
  azure: "/azure.ico",
  datadog: "/datadog.svg",
  grafana: "/grafana.svg",
  netdata: "/netdata.svg",
  splunk: "/splunk.svg",
  pagerduty: "/pagerduty-icon.svg",
  github: "/github-mark.svg",
  slack: "/slack.png",
  jenkins: "/jenkins.svg",
  cloudbees: "/cloudbees.svg",
  kubernetes: "/kubernetes-svgrepo-com.svg",
  kubectl: "/kubernetes-svgrepo-com.svg",
  dynatrace: "/dynatrace.png",
  coroot: "/coroot.svg",
  thousandeyes: "/thousandeyes.svg",
  bigpanda: "/bigpanda.svg",
  confluence: "/confluence.svg",
  sharepoint: "/sharepoint.png",
  bitbucket: "/bitbucket.svg",
  tailscale: "/tailscale.svg",
  scaleway: "/scaleway.svg",
  ovh: "/ovh.svg",
};

const PROVIDER_NAMES: Record<string, string> = {
  aws: "AWS",
  gcp: "Google Cloud",
  azure: "Azure",
  datadog: "Datadog",
  grafana: "Grafana",
  netdata: "Netdata",
  splunk: "Splunk",
  pagerduty: "PagerDuty",
  github: "GitHub",
  slack: "Slack",
  jenkins: "Jenkins",
  cloudbees: "CloudBees",
  kubectl: "Kubernetes",
  dynatrace: "Dynatrace",
  coroot: "Coroot",
  thousandeyes: "ThousandEyes",
  bigpanda: "BigPanda",
  confluence: "Confluence",
  sharepoint: "SharePoint",
  bitbucket: "Bitbucket",
  tailscale: "Tailscale",
  scaleway: "Scaleway",
  ovh: "OVH Cloud",
};

const SEVERITY_STYLES: Record<string, string> = {
  critical: "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20",
  high: "bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/20",
  warning: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/20",
  low: "bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20",
  info: "bg-muted text-muted-foreground border-border",
};

const STATUS_STYLES: Record<string, string> = {
  analyzed: "bg-green-500/10 text-green-600 dark:text-green-400",
  investigating: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400",
  open: "bg-red-500/10 text-red-600 dark:text-red-400",
  resolved: "bg-muted text-muted-foreground",
};

const INITIAL_COUNT = 10;

function EventIcon({ event }: { event: ActivityEvent }) {
  if (event.type === "connector_added" && event.provider) {
    const icon = PROVIDER_ICONS[event.provider];
    if (icon) {
      return (
        <div className="h-8 w-8 rounded-lg bg-background border border-border flex items-center justify-center flex-shrink-0">
          <img src={icon} alt={event.provider} className="h-4 w-4 object-contain" />
        </div>
      );
    }
    return (
      <div className="h-8 w-8 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center flex-shrink-0">
        <Plug className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
      </div>
    );
  }

  if (event.type === "incident_created") {
    const source = event.source?.toLowerCase() || "";
    const icon = PROVIDER_ICONS[source];
    if (icon) {
      return (
        <div className="h-8 w-8 rounded-lg bg-red-500/5 border border-red-500/20 flex items-center justify-center flex-shrink-0">
          <img src={icon} alt={source} className="h-4 w-4 object-contain" />
        </div>
      );
    }
    return (
      <div className="h-8 w-8 rounded-lg bg-red-500/10 border border-red-500/20 flex items-center justify-center flex-shrink-0">
        <AlertTriangle className="h-3.5 w-3.5 text-red-600 dark:text-red-400" />
      </div>
    );
  }

  if (event.type === "member_joined") {
    const initial = (event.name || event.email || "?").charAt(0).toUpperCase();
    return (
      <div className="h-8 w-8 rounded-full bg-muted border border-border flex items-center justify-center flex-shrink-0">
        <span className="text-xs font-medium text-muted-foreground">{initial}</span>
      </div>
    );
  }

  return (
    <div className="h-8 w-8 rounded-lg bg-muted border border-border flex items-center justify-center flex-shrink-0">
      <Clock className="h-3.5 w-3.5 text-muted-foreground" />
    </div>
  );
}

function MemberEvent({ event }: { event: ActivityEvent }) {
  const name = event.name || event.email || "Someone";
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="font-medium text-foreground truncate">{name}</span>
      <span className="text-muted-foreground">joined as</span>
      <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
        event.role === "admin"
          ? "bg-foreground/10 text-foreground"
          : event.role === "editor"
          ? "bg-blue-500/10 text-blue-600 dark:text-blue-400"
          : "bg-muted text-muted-foreground"
      }`}>
        {event.role || "viewer"}
      </span>
    </div>
  );
}

function IncidentEvent({ event }: { event: ActivityEvent }) {
  const severityClass = SEVERITY_STYLES[event.severity || ""] || SEVERITY_STYLES.info;
  const statusClass = STATUS_STYLES[event.status || ""] || STATUS_STYLES.resolved;
  const sourceName = PROVIDER_NAMES[event.source?.toLowerCase() || ""] || event.source || "Unknown";

  return (
    <div className="min-w-0 flex-1">
      <span className="font-medium text-foreground line-clamp-1">{event.title || "Untitled incident"}</span>
      <div className="flex items-center gap-2 mt-1 flex-wrap">
        <span className="text-[10px] text-muted-foreground">{sourceName}</span>
        {event.severity && (
          <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium border ${severityClass}`}>
            {event.severity}
          </span>
        )}
        {event.status && (
          <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${statusClass}`}>
            {event.status}
          </span>
        )}
      </div>
    </div>
  );
}

function ConnectorEvent({ event }: { event: ActivityEvent }) {
  const providerName = PROVIDER_NAMES[event.provider || ""] || event.provider || "Unknown";
  const who = event.description?.split(" connected ")[0] || "Someone";

  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="font-medium text-foreground truncate">{who}</span>
      <span className="text-muted-foreground">connected</span>
      <span className="font-medium text-foreground">{providerName}</span>
    </div>
  );
}

function EventRow({ event }: { event: ActivityEvent }) {
  return (
    <div className="flex items-start gap-3 py-3 border-b border-border/30 last:border-0">
      <EventIcon event={event} />
      <div className="flex-1 min-w-0 text-sm">
        {event.type === "member_joined" && <MemberEvent event={event} />}
        {event.type === "incident_created" && <IncidentEvent event={event} />}
        {event.type === "connector_added" && <ConnectorEvent event={event} />}
        {!["member_joined", "incident_created", "connector_added"].includes(event.type) && (
          <span className="text-foreground/80">{event.description}</span>
        )}
      </div>
      <span className="text-[11px] text-muted-foreground/50 tabular-nums flex-shrink-0 pt-0.5">
        {event.timestamp ? formatRelativeTime(event.timestamp) : ""}
      </span>
    </div>
  );
}

function formatRelativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60000);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);

  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  if (diffDay < 7) return `${diffDay}d ago`;

  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function OrgActivity() {
  const [showAll, setShowAll] = useState(false);

  const { data, isLoading } = useQuery<{ events: ActivityEvent[] }>(
    '/api/orgs/activity',
    jsonFetcher,
    { staleTime: 30_000, retryCount: 2 },
  );

  const events = data?.events ?? [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2 text-sm">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading...
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <Clock className="h-6 w-6 text-muted-foreground/30 mb-2" />
        <p className="text-sm text-muted-foreground">No activity yet</p>
        <p className="text-xs text-muted-foreground/60 mt-0.5">
          Events appear here as your team uses Aurora
        </p>
      </div>
    );
  }

  const displayedEvents = showAll ? events : events.slice(0, INITIAL_COUNT);
  const hasMore = events.length > INITIAL_COUNT;

  let lastDate = "";

  return (
    <div>
      {displayedEvents.map((event, idx) => {
        const eventDate = event.timestamp
          ? new Date(event.timestamp).toLocaleDateString(undefined, {
              month: "long",
              day: "numeric",
              year: "numeric",
            })
          : "";
        const showDate = eventDate !== lastDate;
        if (showDate) lastDate = eventDate;

        return (
          <div key={idx}>
            {showDate && eventDate && (
              <div className="pt-6 pb-1 first:pt-0">
                <span className="text-[11px] font-medium text-muted-foreground/60 uppercase tracking-wider">
                  {eventDate}
                </span>
              </div>
            )}
            <EventRow event={event} />
          </div>
        );
      })}

      {hasMore && (
        <button
          onClick={() => setShowAll(!showAll)}
          className="flex items-center gap-1.5 mx-auto mt-4 text-xs text-muted-foreground hover:text-foreground transition-colors py-2"
        >
          {showAll ? (
            <>
              <ChevronUp className="h-3 w-3" />
              Show less
            </>
          ) : (
            <>
              <ChevronDown className="h-3 w-3" />
              Show {events.length - INITIAL_COUNT} more events
            </>
          )}
        </button>
      )}
    </div>
  );
}
