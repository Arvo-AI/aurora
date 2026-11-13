"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { RabbitMQAccount } from "@/lib/services/rabbitmq";

interface RabbitMQConnectedStatusProps {
  accounts: RabbitMQAccount[];
  /** No account id removes every broker. */
  onDisconnect: (accountId?: string) => Promise<void>;
  loading: boolean;
}

function Field({ label, value, title }: Readonly<{ label: string; value: string; title?: string }>) {
  return (
    <div className="p-3 border rounded-lg min-w-0">
      <p className="text-xs text-muted-foreground uppercase tracking-wide">{label}</p>
      <p className="text-sm font-semibold truncate" title={title ?? value}>{value}</p>
    </div>
  );
}

function BrokerCard({
  account,
  onRemove,
  loading,
  removable,
}: Readonly<{
  account: RabbitMQAccount;
  onRemove: () => Promise<void>;
  loading: boolean;
  removable: boolean;
}>) {
  const tags = account.tags?.length ? account.tags.join(", ") : "none reported";
  const title = account.displayName || account.name || account.clusterName || account.accountId;
  const failed = account.valid === false;
  return (
    <div className="border rounded-lg p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold truncate" title={title}>{title}</p>
          <p className="text-xs text-muted-foreground truncate" title={account.accountId}>{account.accountId}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {account.allowed === false && (
            <span className="text-xs rounded border px-2 py-0.5 text-muted-foreground">Not granted to your team</span>
          )}
          {failed && (
            <span className="text-xs rounded border border-red-300 px-2 py-0.5 text-red-700 dark:text-red-400">Unreachable</span>
          )}
          {removable && (
            <Button variant="outline" size="sm" onClick={onRemove} disabled={loading}>
              Remove
            </Button>
          )}
        </div>
      </div>
      {account.error && (
        <div className="p-3 bg-orange-50 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-800 rounded text-sm text-orange-900 dark:text-orange-300">
          {account.error}
        </div>
      )}
      <div className="grid sm:grid-cols-2 md:grid-cols-3 gap-3">
        <Field label="Management URL" value={account.url || "Unknown"} />
        <Field label="User" value={account.username || "Unknown"} />
        <Field label="Cluster" value={account.clusterName || "Unknown"} />
        <Field label="Version" value={account.version || "Unknown"} />
        <Field label="Default vhost" value={account.vhost || "/"} />
        <Field label="Tags" value={tags} />
      </div>
    </div>
  );
}

export function RabbitMQConnectedStatus({
  accounts,
  onDisconnect,
  loading,
}: Readonly<RabbitMQConnectedStatusProps>) {
  const several = accounts.length > 1;
  const disconnectLabel = several ? "Disconnect all brokers" : "Disconnect RabbitMQ";
  return (
    <Card>
      <CardHeader>
        <CardTitle>{several ? `${accounts.length} RabbitMQ brokers connected` : "RabbitMQ connected"}</CardTitle>
        <CardDescription>
          Aurora can read queue depth, consumers, node alarms, and connections from {several ? "these brokers" : "this broker"}.
          {several && " Investigations query every broker your team can use, or one broker by name."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {accounts.map((account) => (
          <BrokerCard
            key={account.accountId}
            account={account}
            loading={loading}
            removable={several}
            onRemove={() => onDisconnect(account.accountId)}
          />
        ))}
        <div className="flex justify-end">
          <Button variant="outline" onClick={() => onDisconnect()} disabled={loading}>
            {loading ? "Disconnecting…" : disconnectLabel}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
