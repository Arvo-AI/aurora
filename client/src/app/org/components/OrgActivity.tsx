"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import {
  UserPlus,
  Zap,
  Plug,
  Loader2,
  Clock,
} from "lucide-react";

interface ActivityEvent {
  type: string;
  timestamp: string | null;
  description: string;
  [key: string]: unknown;
}

const EVENT_CONFIG: Record<string, { icon: typeof UserPlus; color: string; bg: string }> = {
  member_joined: { icon: UserPlus, color: "text-blue-500", bg: "bg-blue-500/10" },
  incident_created: { icon: Zap, color: "text-orange-500", bg: "bg-orange-500/10" },
  connector_added: { icon: Plug, color: "text-green-500", bg: "bg-green-500/10" },
};

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
  return new Date(dateStr).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

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
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading activity...
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <Clock className="h-10 w-10 text-muted-foreground/40 mb-3" />
        <p className="text-sm font-medium">No activity yet</p>
        <p className="text-xs text-muted-foreground mt-1">
          Events will appear here as your team uses Aurora
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Recent Activity</h2>
        <p className="text-sm text-muted-foreground">
          Latest events across your organization
        </p>
      </div>

      <div className="relative">
        {/* Timeline line */}
        <div className="absolute left-[19px] top-0 bottom-0 w-px bg-border" />

        <div className="space-y-0">
          {events.map((event, idx) => {
            const config = EVENT_CONFIG[event.type] || EVENT_CONFIG.member_joined;
            const Icon = config.icon;
            return (
              <div key={idx} className="relative flex items-start gap-4 py-3 pl-0">
                <div
                  className={`relative z-10 h-10 w-10 rounded-full ${config.bg} flex items-center justify-center flex-shrink-0 border-2 border-background`}
                >
                  <Icon className={`h-4 w-4 ${config.color}`} />
                </div>
                <div className="flex-1 min-w-0 pt-1">
                  <p className="text-sm">{event.description}</p>
                  {event.timestamp && (
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {timeAgo(event.timestamp)}
                      <span className="mx-1.5 text-border">·</span>
                      {new Date(event.timestamp).toLocaleDateString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
