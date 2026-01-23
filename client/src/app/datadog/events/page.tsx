"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { datadogService, DatadogIngestedEvent } from "@/lib/services/datadog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { HelpCircle } from "lucide-react";

export default function DatadogEventsPage() {
  const router = useRouter();
  const [events, setEvents] = useState<DatadogIngestedEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(20);

  const loadEvents = async (newOffset = 0) => {
    try {
      setLoading(true);
      setError(null);
      const params = new URLSearchParams({ limit: String(limit), offset: String(newOffset) });
      const response = await datadogService.getIngestedEvents(params);
      setEvents(response.events);
      setTotal(response.total);
      setOffset(newOffset);
    } catch (err: unknown) {
      console.error("Failed to load Datadog events", err);
      const message = err instanceof Error ? err.message : "Failed to load events";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadEvents();
  }, []);

  const handleNextPage = () => {
    if (offset + limit < total) {
      loadEvents(offset + limit);
    }
  };

  const handlePrevPage = () => {
    if (offset > 0) {
      loadEvents(Math.max(0, offset - limit));
    }
  };

  const formatDate = (value?: string) => {
    if (!value) return 'N/A';
    try {
      return new Date(value).toLocaleString();
    } catch {
      return value;
    }
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Datadog Webhook Events</h1>
          <p className="text-muted-foreground mt-1">Monitor incidents forwarded from Datadog monitors</p>
        </div>
        <div className="flex items-center gap-2">
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="How to test your Datadog webhook">
                <HelpCircle className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-80 text-sm space-y-2" align="end">
              <p className="font-semibold text-foreground">Need to verify the webhook?</p>
              <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                <li>Create or edit a Datadog monitor and add <code className="bg-muted px-1 rounded">@webhook-{"<your_webhook_name>"}</code> to the notification message.</li>
                <li>Use Datadog&apos;s <strong>Test notifications</strong> button (or trigger the monitor) to send a sample payload.</li>
                <li>Keep this page open—events appear within a few seconds after Datadog delivers the alert.</li>
              </ol>
              <p className="text-muted-foreground">Once the test succeeds you&apos;ll see the alert listed below with the full JSON payload.</p>
            </PopoverContent>
          </Popover>
          <Button variant="outline" onClick={() => loadEvents(offset)}>Refresh</Button>
          <Button variant="outline" onClick={() => router.push("/datadog/auth")}>Settings</Button>
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
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <p className="text-muted-foreground">Loading events...</p>
          </CardContent>
        </Card>
      ) : events.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <p className="text-muted-foreground font-medium">No events received yet</p>
            <p className="text-sm text-muted-foreground mt-2">
              Configure a Datadog webhook to forward monitor notifications to Aurora.
            </p>
            <Button variant="outline" className="mt-4" onClick={() => router.push("/datadog/auth")}>Configure Webhook</Button>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="space-y-4">
            {events.map(event => (
              <Card key={event.id}>
                <CardHeader>
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-2">
                        <CardTitle className="text-lg">{event.title || 'Datadog Event'}</CardTitle>
                        {event.status && (
                          <Badge variant="outline">{event.status}</Badge>
                        )}
                        {event.eventType && (
                          <Badge variant="secondary">{event.eventType}</Badge>
                        )}
                      </div>
                      {event.scope && (
                        <CardDescription>Scope: {event.scope}</CardDescription>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Received {formatDate(event.receivedAt)}
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  {event.payload && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-sm font-medium hover:underline">View payload</summary>
                      <pre className="mt-2 p-3 bg-muted rounded text-xs overflow-auto max-h-64">
                        {JSON.stringify(event.payload, null, 2)}
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
                Showing {offset + 1} to {Math.min(offset + limit, total)} of {total} events
              </p>
              <div className="flex gap-2">
                <Button variant="outline" disabled={offset === 0} onClick={handlePrevPage}>Previous</Button>
                <Button variant="outline" disabled={offset + limit >= total} onClick={handleNextPage}>Next</Button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
