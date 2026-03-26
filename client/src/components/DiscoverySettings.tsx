"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { Radar, Check } from "lucide-react";
import { useAuth } from "@/hooks/useAuthHooks";
import { getEnv } from "@/lib/env";

export function DiscoverySettings() {
  const [status, setStatus] = useState<string>("loading");
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [intervalHours, setIntervalHours] = useState<number>(24);
  const [savingInterval, setSavingInterval] = useState(false);
  const { toast } = useToast();
  const { userId } = useAuth();
  const backendUrl = getEnv("NEXT_PUBLIC_BACKEND_URL");

  useEffect(() => {
    fetch("/api/prediscovery/status", { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        setStatus(data.status || "never_run");
        setLastRun(data.updated_at || data.started_at || null);
      })
      .catch(() => setStatus("unknown"));
  }, []);

  useEffect(() => {
    if (!userId || !backendUrl) return;
    fetch(`${backendUrl}/api/user-preferences?key=prediscovery_interval_hours`, {
      headers: { "X-User-ID": userId },
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.value != null) setIntervalHours(data.value);
      })
      .catch(() => {});
  }, [userId, backendUrl]);

  const runDiscovery = async () => {
    setDiscovering(true);
    try {
      const res = await fetch("/api/prediscovery/run", {
        method: "POST",
        credentials: "include",
      });
      if (res.ok) {
        setStatus("in_progress");
        toast({ title: "Discovery started", description: "Scanning your infrastructure in the background." });
      } else {
        toast({ title: "Failed to start", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to start", variant: "destructive" });
    } finally {
      setDiscovering(false);
    }
  };

  const saveInterval = async () => {
    if (!userId || !backendUrl) return;
    const clamped = Math.max(1, Math.round(intervalHours));
    setIntervalHours(clamped);
    setSavingInterval(true);
    try {
      const res = await fetch(`${backendUrl}/api/user-preferences`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-User-ID": userId },
        body: JSON.stringify({ key: "prediscovery_interval_hours", value: clamped }),
      });
      if (res.ok) {
        toast({ title: "Saved", description: `Discovery will run every ${clamped} hour${clamped === 1 ? "" : "s"}.` });
      } else {
        toast({ title: "Failed to save", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to save", variant: "destructive" });
    } finally {
      setSavingInterval(false);
    }
  };

  const formatStatus = () => {
    if (status === "loading") return "Checking...";
    if (status === "in_progress") return "In progress...";
    if (status === "completed" && lastRun) {
      const d = new Date(lastRun);
      return `Last synced ${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZoneName: "short" })}`;
    }
    if (status === "failed") return "Last run failed";
    if (status === "never_run") return "Never run";
    return status;
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Infrastructure Discovery</CardTitle>
        <CardDescription>
          Automatically scan connected integrations to map how your services, pipelines,
          and monitoring are interconnected. Results are used to provide context during investigations.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between p-4 border rounded-lg">
          <div className="space-y-1">
            <h4 className="font-medium">Run Discovery</h4>
            <p className="text-sm text-muted-foreground">{formatStatus()}</p>
          </div>
          <Button
            variant="outline"
            onClick={runDiscovery}
            disabled={discovering || status === "in_progress"}
          >
            <Radar className={`h-4 w-4 mr-2 ${discovering || status === "in_progress" ? "animate-spin" : ""}`} />
            {discovering || status === "in_progress" ? "Discovering..." : "Run Now"}
          </Button>
        </div>

        <div className="flex items-center justify-between p-4 border rounded-lg">
          <div className="space-y-1">
            <Label htmlFor="discovery-interval" className="font-medium">Auto-Discovery Interval</Label>
            <p className="text-sm text-muted-foreground">
              How often to automatically scan (minimum 1 hour)
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Input
              id="discovery-interval"
              type="number"
              min={1}
              value={intervalHours}
              onChange={(e) => setIntervalHours(Number(e.target.value))}
              className="w-20"
            />
            <span className="text-sm text-muted-foreground">hours</span>
            <Button variant="outline" size="sm" onClick={saveInterval} disabled={savingInterval}>
              <Check className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
