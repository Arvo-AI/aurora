"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { dynatraceService, DynatraceAlert } from "@/lib/services/dynatrace";

export default function DynatraceAlertsPage() {
  const router = useRouter();
  const [alerts, setAlerts] = useState<DynatraceAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(20);

  const loadAlerts = async (newOffset = 0) => {
    try {
      setLoading(true);
      setError(null);
      const response = await dynatraceService.getAlerts(limit, newOffset);
      setAlerts(response.alerts);
      setTotal(response.total);
      setOffset(newOffset);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load alerts");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadAlerts(); }, []);

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return "N/A";
    try { return new Date(dateStr).toLocaleString(); } catch { return dateStr; }
  };

  const getSeverityColor = (severity?: string) => {
    const s = severity?.toLowerCase();
    if (s === "critical" || s === "availability") return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
    if (s === "high" || s === "error") return "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200";
    if (s === "medium" || s === "performance" || s === "resource_contention") return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200";
    if (s === "low" || s === "custom_alert") return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
    return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Dynatrace Problems</h1>
          <p className="text-muted-foreground mt-1">Monitor your Dynatrace problem notifications</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => loadAlerts(offset)}>Refresh</Button>
          <Button variant="outline" onClick={() => router.push("/dynatrace/auth")}>Settings</Button>
        </div>
      </div>

      {error && (
        <Card className="mb-6 border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">{error}</p>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <Card><CardContent className="pt-6 text-center py-12"><p className="text-muted-foreground">Loading problems...</p></CardContent></Card>
      ) : alerts.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <p className="text-muted-foreground font-medium">No problems received yet</p>
            <p className="text-sm text-muted-foreground mt-2">Configure a webhook problem notification in Dynatrace to start receiving alerts</p>
            <Button variant="outline" className="mt-4" onClick={() => router.push("/dynatrace/auth")}>Configure Webhook</Button>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="space-y-4">
            {alerts.map((alert) => (
              <Card key={alert.id}>
                <CardHeader>
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-2">
                        <CardTitle className="text-lg">{alert.title || "Untitled Problem"}</CardTitle>
                        <span className={`px-2 py-1 rounded text-xs font-medium ${getSeverityColor(alert.severity)}`}>
                          {alert.severity || "unknown"}
                        </span>
                      </div>
                      {alert.impactedEntity && <CardDescription>Entity: {alert.impactedEntity}</CardDescription>}
                    </div>
                    {alert.problemUrl && (
                      <a href={alert.problemUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-blue-600 hover:underline">
                        View in Dynatrace
                      </a>
                    )}
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div>
                      <span className="text-muted-foreground">Received:</span>
                      <span className="ml-2">{formatDate(alert.receivedAt)}</span>
                    </div>
                    {alert.impact && (
                      <div>
                        <span className="text-muted-foreground">Impact:</span>
                        <span className="ml-2">{alert.impact}</span>
                      </div>
                    )}
                    {alert.tags && (
                      <div className="col-span-2">
                        <span className="text-muted-foreground">Tags:</span>
                        <span className="ml-2">{alert.tags}</span>
                      </div>
                    )}
                  </div>
                  {alert.payload && Object.keys(alert.payload).length > 0 && (
                    <details className="mt-4">
                      <summary className="cursor-pointer text-sm font-medium hover:underline">View full payload</summary>
                      <pre className="mt-2 p-3 bg-muted rounded text-xs overflow-auto max-h-64">{JSON.stringify(alert.payload, null, 2)}</pre>
                    </details>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>

          {total > limit && (
            <div className="flex items-center justify-between mt-6">
              <p className="text-sm text-muted-foreground">
                Showing {offset + 1} to {Math.min(offset + limit, total)} of {total} problems
              </p>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => loadAlerts(Math.max(0, offset - limit))} disabled={offset === 0}>Previous</Button>
                <Button variant="outline" onClick={() => loadAlerts(offset + limit)} disabled={offset + limit >= total}>Next</Button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
