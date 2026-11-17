"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/hooks/use-toast";
import { useQuery, jsonFetcher } from "@/lib/query";
import { Eye, Loader2 } from "lucide-react";

interface WatchFinding {
  subject: string;
  severity: "low" | "medium" | "high" | "critical";
  summary: string;
}

interface WatchCluster {
  cluster_id: string;
  name: string | null;
  source: string;
  polls: number;
  learning: boolean;
  last_poll_at: string | null;
  last_error: string | null;
  blind_spots: string[];
  open_findings: WatchFinding[];
}

interface WatchStatus {
  available: boolean;
  enabled: boolean;
  namespaces: string[] | null;
  acting_as_you: boolean;
  poll_seconds: number;
  learning_polls: number;
  clusters: WatchCluster[];
}

const SEVERITY_CLASS: Record<WatchFinding["severity"], string> = {
  critical: "border-red-700 text-red-400",
  high: "border-orange-700 text-orange-400",
  medium: "border-yellow-700 text-yellow-400",
  low: "border-zinc-700 text-zinc-400",
};

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 90) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)} min ago`;
}

export function AuroraWatchCard() {
  const { toast } = useToast();
  const { data, error, isLoading, mutate } = useQuery<WatchStatus>("/api/aurora-watch", jsonFetcher, {
    staleTime: 10_000,
    refreshInterval: 30_000,
    retryCount: 1,
  });
  const [saving, setSaving] = useState(false);
  const [namespaces, setNamespaces] = useState("");
  const [edited, setEdited] = useState(false);

  // Follow the server's value until the user starts typing (the status refreshes every 30s).
  useEffect(() => {
    if (data && !edited) setNamespaces((data.namespaces || []).join(", "));
  }, [data, edited]);

  const save = async (enabled: boolean) => {
    setSaving(true);
    try {
      const list = namespaces.split(",").map((n) => n.trim()).filter(Boolean);
      const res = await fetch("/api/aurora-watch", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ enabled, namespaces: list.length ? list : null }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast({ title: "Could not update Aurora Watch", description: body.error, variant: "destructive" });
        return;
      }
      toast({ title: enabled ? "Aurora Watch is on" : "Aurora Watch is off" });
      setEdited(false);
      await mutate();
    } finally {
      setSaving(false);
    }
  };

  if (isLoading && !data) {
    return (
      <Card className="bg-zinc-950 border-zinc-800 mt-6">
        <CardContent className="flex items-center justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin text-zinc-500" />
        </CardContent>
      </Card>
    );
  }
  if (error && !data) {
    // Status could not load; turning Watch off does not depend on it.
    return (
      <Card className="bg-zinc-950 border-zinc-800 mt-6">
        <CardHeader>
          <CardTitle className="text-white text-xl flex items-center gap-2">
            <Eye className="h-5 w-5" /> Aurora Watch
          </CardTitle>
          <CardDescription className="text-red-400 mt-2">Could not load Aurora Watch status.</CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => mutate()} className="border-zinc-700 hover:bg-zinc-900">
            Retry
          </Button>
          <Button variant="ghost" size="sm" disabled={saving} onClick={() => save(false)}
                  className="text-red-400 hover:text-red-300 hover:bg-red-950/20">
            Turn off Aurora Watch
          </Button>
        </CardContent>
      </Card>
    );
  }
  if (!data || !data.available) return null;

  const learningPolls = data.learning_polls || 5;
  return (
    <Card className="bg-zinc-950 border-zinc-800 mt-6">
      <CardHeader>
        <div className="flex items-start justify-between gap-6">
          <div>
            <CardTitle className="text-white text-xl flex items-center gap-2">
              <Eye className="h-5 w-5" /> Aurora Watch
            </CardTitle>
            <CardDescription className="text-zinc-400 mt-2 max-w-3xl">
              Aurora reads these clusters every{" "}
              {data.poll_seconds === 60 ? "minute" : `${Math.round(data.poll_seconds / 60)} minutes`} with the same read-only
              access its investigations use, learns what normal looks like, and opens an incident when something
              regresses that no alert rule caught: a Service that loses its endpoints, pods that cannot start, crash
              loops, new errors in logs. Each incident names the change that most likely caused it and starts an
              investigation. Nothing is configured per alert, and Aurora never writes to the cluster.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {saving && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
            <Switch checked={data.enabled} disabled={saving} onCheckedChange={(v) => save(v)} aria-label="Aurora Watch" />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-end gap-2 max-w-xl">
          <div className="flex-1">
            <Label htmlFor="watch-namespaces" className="text-zinc-400 text-xs">
              Namespaces to watch (comma-separated; empty = all except Kubernetes&apos; own)
            </Label>
            <Input
              id="watch-namespaces"
              value={namespaces}
              onChange={(e) => { setNamespaces(e.target.value); setEdited(true); }}
              placeholder="payments, checkout"
              className="bg-zinc-900 border-zinc-800 mt-1"
            />
          </div>
          <Button variant="outline" size="sm" disabled={saving || !data.enabled} onClick={() => save(true)}
                  className="border-zinc-700 hover:bg-zinc-900">
            Save
          </Button>
        </div>

        {data.enabled && data.clusters.length === 0 && (
          <p className="text-sm text-zinc-500">Connect a cluster above and Aurora Watch starts on the next minute.</p>
        )}
        {data.enabled && data.clusters.map((c) => (
          <div key={c.cluster_id} className="p-3 bg-zinc-900 border border-zinc-800 rounded-lg text-sm">
            <div className="flex items-center gap-3">
              <span className="text-white font-medium">{c.name || c.cluster_id}</span>
              {c.last_error ? (
                <Badge variant="outline" className="border-red-700 text-red-400">Not readable</Badge>
              ) : c.learning ? (
                <Badge variant="outline" className="border-blue-700 text-blue-400">
                  Learning ({Math.min(c.polls, learningPolls)}/{learningPolls})
                </Badge>
              ) : (
                <Badge variant="outline" className="border-green-700 text-green-400">Watching</Badge>
              )}
              <span className="text-zinc-500">last look {timeAgo(c.last_poll_at)}</span>
            </div>
            {c.last_error && <p className="mt-2 text-red-400">{c.last_error}</p>}
            {c.open_findings.length > 0 && (
              <ul className="mt-2 space-y-1">
                {c.open_findings.map((f) => (
                  <li key={f.subject} className="flex items-center gap-2 text-zinc-300">
                    <Badge variant="outline" className={SEVERITY_CLASS[f.severity]}>{f.severity}</Badge>
                    {f.summary}
                  </li>
                ))}
              </ul>
            )}
            {c.blind_spots.length > 0 && (
              <p className="mt-2 text-xs text-zinc-500">Not visible to Aurora: {c.blind_spots.join("; ")}</p>
            )}
          </div>
        ))}
        {data.enabled && !data.acting_as_you && (
          <p className="text-xs text-zinc-500">
            Aurora Watch acts as the person who turned it on. Turning it off and on again makes it act as you.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
