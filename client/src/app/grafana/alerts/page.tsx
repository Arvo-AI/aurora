"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { grafanaService, GrafanaAlert } from "@/lib/services/grafana";

export default function GrafanaAlertsPage() {
  const router = useRouter();
  const [alerts, setAlerts] = useState<GrafanaAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(20);

  const loadAlerts = async (newOffset = 0) => {
    try {
      setLoading(true);
      setError(null);
      const response = await grafanaService.getAlerts(limit, newOffset);
      setAlerts(response.alerts);
      setTotal(response.total);
      setOffset(newOffset);
    } catch (err: any) {
      console.error("Failed to load alerts", err);
      setError(err?.message || "Failed to load alerts");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAlerts();
  }, []);

  const handleNextPage = () => {
    if (offset + limit < total) {
      loadAlerts(offset + limit);
    }
  };

  const handlePrevPage = () => {
    if (offset > 0) {
      loadAlerts(Math.max(0, offset - limit));
    }
  };

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return "N/A";
    try {
      const date = new Date(dateStr);
      return date.toLocaleString();
    } catch {
      return dateStr;
    }
  };

  const getStateBadgeColor = (state?: string) => {
    const lowerState = state?.toLowerCase();
    if (lowerState === "firing" || lowerState === "alerting") return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
    if (lowerState === "resolved" || lowerState === "ok") return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
    if (lowerState === "pending") return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200";
    return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Grafana Alerts</h1>
          <p className="text-muted-foreground mt-1">Monitor your Grafana alert notifications</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => loadAlerts(offset)}>
            Refresh
          </Button>
          <Button variant="outline" onClick={() => router.push("/grafana/auth")}>
            Settings
          </Button>
        </div>
      </div>

      {error && (
        <Card className="mb-6 border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive flex items-center gap-2">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {error}
            </p>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <p className="text-muted-foreground">Loading alerts...</p>
          </CardContent>
        </Card>
      ) : alerts.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <svg className="h-12 w-12 mx-auto mb-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
            </svg>
            <p className="text-muted-foreground font-medium">No alerts received yet</p>
            <p className="text-sm text-muted-foreground mt-2">
              Configure the webhook in Grafana to start receiving alerts
            </p>
            <Button variant="outline" className="mt-4" onClick={() => router.push("/grafana/auth")}>
              Configure Webhook
            </Button>
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
                        <CardTitle className="text-lg">{alert.title || "Untitled Alert"}</CardTitle>
                        <span className={`px-2 py-1 rounded text-xs font-medium ${getStateBadgeColor(alert.state)}`}>
                          {alert.state || "unknown"}
                        </span>
                      </div>
                      {alert.ruleName && (
                        <CardDescription>Rule: {alert.ruleName}</CardDescription>
                      )}
                    </div>
                    <div className="flex gap-2">
                      {alert.ruleUrl && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => window.open(alert.ruleUrl, "_blank")}
                        >
                          <svg className="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                          </svg>
                          Rule
                        </Button>
                      )}
                      {alert.dashboardUrl && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => window.open(alert.dashboardUrl, "_blank")}
                        >
                          <svg className="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                          </svg>
                          Dashboard
                        </Button>
                      )}
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div>
                      <span className="text-muted-foreground">Received:</span>
                      <span className="ml-2">{formatDate(alert.receivedAt)}</span>
                    </div>
                    {alert.alertUid && (
                      <div>
                        <span className="text-muted-foreground">Alert UID:</span>
                        <span className="ml-2 font-mono text-xs">{alert.alertUid}</span>
                      </div>
                    )}
                  </div>
                  {alert.payload && Object.keys(alert.payload).length > 0 && (
                    <details className="mt-4">
                      <summary className="cursor-pointer text-sm font-medium hover:underline">
                        View full payload
                      </summary>
                      <pre className="mt-2 p-3 bg-muted rounded text-xs overflow-auto max-h-64">
                        {JSON.stringify(alert.payload, null, 2)}
                      </pre>
                    </details>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>

          {total > limit && (
            <div className="flex items-center justify-between mt-6">
              <p className="text-sm text-muted-foreground">
                Showing {offset + 1} to {Math.min(offset + limit, total)} of {total} alerts
              </p>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  onClick={handlePrevPage}
                  disabled={offset === 0}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  onClick={handleNextPage}
                  disabled={offset + limit >= total}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
