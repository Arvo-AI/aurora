"use client";

import { ReactNode, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ExternalLink } from "lucide-react";
import { elasticService, ElasticAlert, isHttpUrl } from "@/lib/services/elastic";
import { isElasticEnabled } from "@/lib/feature-flags";
import { ConnectorNotEnabled } from "@/components/elastic/ConnectorNotEnabled";

const PAGE_SIZE = 20;

function severityBadge(severity?: string) {
  const s = severity?.toLowerCase();
  if (s === "critical") return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
  if (s === "high") return "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200";
  if (s === "medium") return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200";
  if (s === "low") return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
  return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
}

function stateBadge(state?: string) {
  if (state === "recovered") return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
  if (state === "active") return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
  return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
}

function formatDate(dateStr?: string) {
  if (!dateStr) return "N/A";
  try {
    return new Date(dateStr).toLocaleString();
  } catch {
    return dateStr;
  }
}

export default function ElasticAlertsPage() {
  const router = useRouter();
  const enabled = isElasticEnabled();
  const [alerts, setAlerts] = useState<ElasticAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [stateFilter, setStateFilter] = useState<string>("");
  // Filter/page clicks can overlap; only the newest request may update state.
  const requestSeq = useRef(0);

  const loadAlerts = async (newOffset = 0, state = stateFilter) => {
    const seq = ++requestSeq.current;
    try {
      setLoading(true);
      setError(null);
      const response = await elasticService.getAlerts(PAGE_SIZE, newOffset, state || undefined);
      if (seq !== requestSeq.current) return;
      setAlerts(response.alerts);
      setTotal(response.total);
      setOffset(newOffset);
    } catch (err: unknown) {
      if (seq !== requestSeq.current) return;
      console.error("Failed to load alerts", err);
      setError(err instanceof Error ? err.message : "Failed to load alerts");
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (!enabled) return;
    loadAlerts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  if (!enabled) return <ConnectorNotEnabled />;

  const changeFilter = (state: string) => {
    setStateFilter(state);
    loadAlerts(0, state);
  };

  let content: ReactNode;
  if (loading) {
    content = (
      <Card>
        <CardContent className="pt-6 text-center py-12">
          <p className="text-muted-foreground">Loading alerts...</p>
        </CardContent>
      </Card>
    );
  } else if (alerts.length === 0) {
    content = (
      <Card>
        <CardContent className="pt-6 text-center py-12">
          <p className="text-muted-foreground font-medium">No alerts received yet</p>
          <p className="text-sm text-muted-foreground mt-2">
            Attach the Aurora Webhook connector to a Kibana rule to start receiving alerts
          </p>
          <Button variant="outline" className="mt-4" onClick={() => router.push("/elastic/auth")}>
            Configure Webhook
          </Button>
        </CardContent>
      </Card>
    );
  } else {
    content = (
      <>
        <div className="space-y-4">
          {alerts.map((alert) => (
            <Card key={alert.id}>
              <CardHeader>
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-2 flex-wrap">
                      <CardTitle className="text-lg">{alert.title || alert.ruleName || "Untitled Alert"}</CardTitle>
                      <span className={`px-2 py-1 rounded text-xs font-medium ${stateBadge(alert.state)}`}>{alert.state || "unknown"}</span>
                      <span className={`px-2 py-1 rounded text-xs font-medium ${severityBadge(alert.severity)}`}>{alert.severity || "unknown"}</span>
                    </div>
                    {alert.ruleName && alert.ruleName !== alert.title && (
                      <CardDescription>Rule: {alert.ruleName}</CardDescription>
                    )}
                    {alert.reason && <CardDescription className="mt-1">{alert.reason}</CardDescription>}
                  </div>
                  {isHttpUrl(alert.viewInAppUrl) && (
                    <a
                      href={alert.viewInAppUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-sm text-blue-600 hover:underline shrink-0"
                    >
                      Open in Kibana <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </div>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                  <div>
                    <span className="text-muted-foreground">Received:</span>
                    <span className="ml-2">{formatDate(alert.receivedAt)}</span>
                  </div>
                  {alert.ruleType && (
                    <div className="min-w-0">
                      <span className="text-muted-foreground">Rule type:</span>
                      <span className="ml-2 font-mono text-xs">{alert.ruleType}</span>
                    </div>
                  )}
                  {alert.actionGroup && (
                    <div>
                      <span className="text-muted-foreground">Action group:</span>
                      <span className="ml-2 font-mono text-xs">{alert.actionGroup}</span>
                    </div>
                  )}
                  {alert.alertUuid && (
                    <div className="min-w-0 overflow-hidden">
                      <span className="text-muted-foreground">Alert UUID:</span>
                      <span className="ml-2 font-mono text-xs truncate block" title={alert.alertUuid}>{alert.alertUuid}</span>
                    </div>
                  )}
                </div>
                {alert.payload && Object.keys(alert.payload).length > 0 && (
                  <details className="mt-4">
                    <summary className="cursor-pointer text-sm font-medium hover:underline">View full payload</summary>
                    <pre className="mt-2 p-3 bg-muted rounded text-xs overflow-auto max-h-64">
                      {JSON.stringify(alert.payload, null, 2)}
                    </pre>
                  </details>
                )}
              </CardContent>
            </Card>
          ))}
        </div>

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between mt-6">
            <p className="text-sm text-muted-foreground">
              Showing {offset + 1} to {Math.min(offset + PAGE_SIZE, total)} of {total} alerts
            </p>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => loadAlerts(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
                Previous
              </Button>
              <Button variant="outline" onClick={() => loadAlerts(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total}>
                Next
              </Button>
            </div>
          </div>
        )}
      </>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold">Elastic Alerts</h1>
          <p className="text-muted-foreground mt-1">Kibana alerts received through the Aurora webhook</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <div className="inline-flex rounded-md border overflow-hidden text-sm">
            {[
              { value: "", label: "All" },
              { value: "active", label: "Active" },
              { value: "recovered", label: "Recovered" },
            ].map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => changeFilter(opt.value)}
                aria-pressed={stateFilter === opt.value}
                className={`px-3 py-1.5 ${stateFilter === opt.value ? "bg-primary text-primary-foreground" : "bg-background text-muted-foreground hover:text-foreground"}`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <Button variant="outline" onClick={() => loadAlerts(offset)}>Refresh</Button>
          <Button variant="outline" onClick={() => router.push("/elastic/auth")}>Settings</Button>
        </div>
      </div>

      {error && (
        <Card className="mb-6 border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">{error}</p>
          </CardContent>
        </Card>
      )}

      {content}
    </div>
  );
}
