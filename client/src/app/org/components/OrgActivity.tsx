"use client";

import { useEffect, useState } from "react";
import { Loader2, Clock } from "lucide-react";

interface ActivityEvent {
  type: string;
  timestamp: string | null;
  description: string;
  [key: string]: unknown;
}

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diff = now - then;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const TYPE_LABELS: Record<string, string> = {
  member_joined: "joined",
  incident_created: "incident",
  connector_added: "connected",
};

export default function OrgActivity() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/orgs/activity")
      .then((r) => r.json())
      .then((data) => setEvents(data.events || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
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

  let lastDate = "";

  return (
    <div className="text-sm">
      {events.map((event, idx) => {
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
              <div className="pt-6 pb-2 first:pt-0">
                <span className="text-[11px] font-medium text-muted-foreground/60 uppercase tracking-wider">
                  {eventDate}
                </span>
              </div>
            )}
            <div className="flex items-baseline gap-3 py-2 border-b border-border/30 last:border-0">
              <span className="text-xs text-muted-foreground/50 tabular-nums w-14 text-right flex-shrink-0">
                {event.timestamp
                  ? new Date(event.timestamp).toLocaleTimeString(undefined, {
                      hour: "numeric",
                      minute: "2-digit",
                    })
                  : ""}
              </span>
              <span className="inline-block text-[10px] font-medium text-muted-foreground bg-muted px-1.5 py-0.5 rounded w-16 text-center flex-shrink-0">
                {TYPE_LABELS[event.type] || event.type}
              </span>
              <span className="text-foreground/80">{event.description}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
