"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { CorootStatus } from "@/lib/services/coroot";

interface CorootConnectedStatusProps {
  status: CorootStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function CorootConnectedStatus({
  status,
  onDisconnect,
  loading,
}: CorootConnectedStatusProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Coroot Connected</CardTitle>
        <CardDescription>
          Aurora is connected to your Coroot instance and can access observability data
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid md:grid-cols-3 gap-4">
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Instance URL</p>
            <p className="text-base font-semibold truncate" title={status.url}>
              {status.url || "Unknown"}
            </p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Email</p>
            <p className="text-base font-semibold truncate" title={status.email}>
              {status.email || "Not provided"}
            </p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Projects</p>
            <p className="text-base font-semibold">
              {status.projects?.length ?? 0} discovered
            </p>
          </div>
        </div>

        {status.projects && status.projects.length > 0 && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Discovered Projects</p>
            <div className="flex flex-wrap gap-2">
              {status.projects.map((project) => (
                <span
                  key={project.id}
                  className="inline-flex items-center px-3 py-1 rounded-full bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-200 dark:border-emerald-800 text-xs font-medium text-emerald-800 dark:text-emerald-300"
                >
                  {project.name || project.id}
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="border rounded-lg p-4 bg-muted/40 space-y-2 text-sm text-muted-foreground">
          <p className="font-medium text-foreground">Available Data</p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            {[
              "Metrics (PromQL)",
              "Logs",
              "Traces",
              "Incidents & RCA",
              "Service Maps",
              "Profiling",
              "Deployments",
              "Nodes",
              "Costs",
              "Risks",
            ].map((item) => (
              <div key={item} className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                {item}
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="text-xs text-muted-foreground">
            Connected since{" "}
            <strong>
              {status.validatedAt
                ? new Date(status.validatedAt).toLocaleString()
                : "just now"}
            </strong>
          </div>
          <Button variant="outline" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting\u2026" : "Disconnect Coroot"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
