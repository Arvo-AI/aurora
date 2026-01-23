"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { splunkService, SplunkAlert } from "@/lib/services/splunk";

export default function SplunkAlertsPage() {
  const router = useRouter();
  const [alerts, setAlerts] = useState<SplunkAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(20);

  const loadAlerts = async (newOffset = 0) => {
    try {
      setLoading(true);
      setError(null);
      const response = await splunkService.getAlerts(limit, newOffset);
      setAlerts(response.alerts);
      setTotal(response.total);
      setOffset(newOffset);
    } catch (err: unknown) {
      console.error("Failed to load alerts", err);
      const errorMessage = err instanceof Error ? err.message : "Failed to load alerts";
      setError(errorMessage);
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

  const getSeverityBadgeColor = (severity?: string) => {
    const lowerSeverity = severity?.toLowerCase();
    if (lowerSeverity === "critical") return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
    if (lowerSeverity === "high") return "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200";
    if (lowerSeverity === "medium") return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200";
    if (lowerSeverity === "low") return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
    return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Splunk Alerts</h1>
          <p className="text-muted-foreground mt-1">Monitor your Splunk alert notifications</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/splunk/search")}>
            Search
          </Button>
          <Button variant="outline" onClick={() => loadAlerts(offset)}>
            Refresh
          </Button>
          <Button variant="outline" onClick={() => router.push("/splunk/auth")}>
            Settings
          </Button>
        </div>
      </div>

      {error && (
        <Card className="mb-6 border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive flex items-center gap-2">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
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
            <svg className="h-12 w-12 mx-auto mb-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
            </svg>
            <p className="text-muted-foreground font-medium">No alerts received yet</p>
            <p className="text-sm text-muted-foreground mt-2">
              Configure a webhook alert action in Splunk to start receiving alerts
            </p>
            <Button variant="outline" className="mt-4" onClick={() => router.push("/splunk/auth")}>
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
                        <span className={`px-2 py-1 rounded text-xs font-medium ${getSeverityBadgeColor(alert.severity)}`}>
                          {alert.severity || "unknown"}
                        </span>
                      </div>
                      {alert.searchName && (
                        <CardDescription>Search: {alert.searchName}</CardDescription>
                      )}
                    </div>
                    <div className="text-sm text-muted-foreground">
                      {alert.resultCount !== undefined && (
                        <span>{alert.resultCount} results</span>
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
                    {alert.alertId && (
                      <div className="min-w-0 overflow-hidden">
                        <span className="text-muted-foreground">Alert ID:</span>
                        <span className="ml-2 font-mono text-xs truncate block xl:whitespace-normal xl:overflow-visible" title={alert.alertId}>
                          {alert.alertId}
                        </span>
                      </div>
                    )}
                  </div>
                  {alert.searchQuery && (
                    <div className="mt-3">
                      <span className="text-sm text-muted-foreground">Query:</span>
                      <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-auto">
                        {alert.searchQuery}
                      </pre>
                    </div>
                  )}
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
