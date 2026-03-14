"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Plug, ArrowRight, CheckCircle2, XCircle, Loader2 } from "lucide-react";
import Link from "next/link";

interface ConnectorStatus {
  id: string;
  name: string;
  iconPath?: string;
  category: string;
  connected: boolean;
  checking: boolean;
}

const CONNECTORS: { id: string; name: string; iconPath: string; category: string }[] = [
  { id: "grafana", name: "Grafana", iconPath: "/grafana.svg", category: "Monitoring" },
  { id: "datadog", name: "Datadog", iconPath: "/datadog.svg", category: "Monitoring" },
  { id: "pagerduty", name: "PagerDuty", iconPath: "/pagerduty-icon.svg", category: "Incident Management" },
  { id: "netdata", name: "Netdata", iconPath: "/netdata.svg", category: "Monitoring" },
  { id: "splunk", name: "Splunk", iconPath: "/splunk.svg", category: "Monitoring" },
  { id: "github", name: "GitHub", iconPath: "/github-mark.svg", category: "Development" },
  { id: "jenkins", name: "Jenkins", iconPath: "/jenkins.svg", category: "CI/CD" },
  { id: "gcp", name: "Google Cloud", iconPath: "/google-cloud-svgrepo-com.svg", category: "Infrastructure" },
  { id: "aws", name: "AWS", iconPath: "/aws.ico", category: "Infrastructure" },
  { id: "azure", name: "Azure", iconPath: "/azure.ico", category: "Infrastructure" },
  { id: "kubectl", name: "Kubernetes", iconPath: "/kubernetes-svgrepo-com.svg", category: "Infrastructure" },
];

export default function OrgIntegrations() {
  const [connectors, setConnectors] = useState<ConnectorStatus[]>(
    CONNECTORS.map((c) => ({ ...c, connected: false, checking: true }))
  );

  useEffect(() => {
    fetch("/api/connectors/status", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { connectors: {} }))
      .then((data) => {
        const statuses: Record<string, { connected?: boolean }> = data.connectors || {};
        setConnectors((prev) =>
          prev.map((c) => ({
            ...c,
            connected: statuses[c.id]?.connected === true,
            checking: false,
          }))
        );
      })
      .catch(() => {
        setConnectors((prev) => prev.map((c) => ({ ...c, checking: false })));
      });
  }, []);

  const connected = connectors.filter((c) => c.connected);
  const available = connectors.filter((c) => !c.connected && !c.checking);
  const loading = connectors.some((c) => c.checking);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Connected Integrations</h2>
          <p className="text-sm text-muted-foreground">
            {connected.length} of {connectors.length} integrations active
          </p>
        </div>
        <Link href="/connectors">
          <Button variant="outline" size="sm" className="gap-1.5">
            Manage
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </Link>
      </div>

      {connected.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {connected.map((c) => (
            <Card key={c.id} className="border-green-500/20 bg-green-500/5">
              <CardContent className="p-4 flex items-center gap-3">
                <div className="h-9 w-9 rounded-lg bg-background border border-border flex items-center justify-center flex-shrink-0">
                  {c.iconPath ? (
                    <img src={c.iconPath} alt={c.name} className="h-5 w-5" />
                  ) : (
                    <Plug className="h-4 w-4" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{c.name}</p>
                  <div className="flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                    <CheckCircle2 className="h-3 w-3" />
                    Connected
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {available.length > 0 && (
        <div className="space-y-4">
          <h3 className="text-sm font-medium text-muted-foreground">Available Integrations</h3>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
            {available.map((c) => (
              <Card key={c.id} className="border-border/50 opacity-60">
                <CardContent className="p-4 flex items-center gap-3">
                  <div className="h-9 w-9 rounded-lg bg-muted border border-border flex items-center justify-center flex-shrink-0">
                    {c.iconPath ? (
                      <img src={c.iconPath} alt={c.name} className="h-5 w-5 grayscale" />
                    ) : (
                      <Plug className="h-4 w-4 text-muted-foreground" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate">{c.name}</p>
                    <div className="flex items-center gap-1 text-xs text-muted-foreground">
                      <XCircle className="h-3 w-3" />
                      Not connected
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {loading && (
        <div className="flex items-center justify-center py-8 text-sm text-muted-foreground gap-2">
          <Loader2 className="h-4 w-4 animate-spin" />
          Checking integration statuses...
        </div>
      )}
    </div>
  );
}
