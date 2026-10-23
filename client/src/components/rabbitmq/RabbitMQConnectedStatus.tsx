"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { RabbitMQStatus } from "@/lib/services/rabbitmq";

interface RabbitMQConnectedStatusProps {
  status: RabbitMQStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function RabbitMQConnectedStatus({
  status,
  onDisconnect,
  loading,
}: Readonly<RabbitMQConnectedStatusProps>) {
  const tags = status.tags?.length ? status.tags.join(", ") : "none reported";
  return (
    <Card>
      <CardHeader>
        <CardTitle>RabbitMQ connected</CardTitle>
        <CardDescription>
          Aurora can read queue depth, consumers, node alarms, and connections from this broker.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid md:grid-cols-3 gap-4">
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Management URL</p>
            <p className="text-base font-semibold truncate" title={status.url}>{status.url || "Unknown"}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">User</p>
            <p className="text-base font-semibold truncate">{status.username || "Unknown"}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Cluster</p>
            <p className="text-base font-semibold truncate">{status.clusterName || "Unknown"}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Version</p>
            <p className="text-base font-semibold">{status.version || "Unknown"}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Default vhost</p>
            <p className="text-base font-semibold">{status.vhost || "/"}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Tags</p>
            <p className="text-base font-semibold truncate" title={tags}>{tags}</p>
          </div>
        </div>
        {status.verifyTls === false && (
          <p className="text-sm text-orange-700 dark:text-orange-400">
            TLS certificate verification is disabled for this broker.
          </p>
        )}
        <div className="flex justify-end">
          <Button variant="outline" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting…" : "Disconnect RabbitMQ"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
