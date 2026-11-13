"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface RabbitMQConnectionStepProps {
  url: string;
  setUrl: (value: string) => void;
  username: string;
  setUsername: (value: string) => void;
  password: string;
  setPassword: (value: string) => void;
  vhost: string;
  setVhost: (value: string) => void;
  name: string;
  setName: (value: string) => void;
  /** A broker is already connected: skip the setup steps and add one more. */
  addAnother?: boolean;
  loading: boolean;
  onConnect: (event: React.FormEvent<HTMLFormElement>) => void;
}

export function RabbitMQConnectionStep({
  url,
  setUrl,
  username,
  setUsername,
  password,
  setPassword,
  vhost,
  setVhost,
  name,
  setName,
  addAnother = false,
  loading,
  onConnect,
}: Readonly<RabbitMQConnectionStepProps>) {
  const submitLabel = addAnother ? "Add broker" : "Connect RabbitMQ";
  return (
    <Card>
      <CardContent className="space-y-6 pt-6">
        {!addAnother && (
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Prerequisites</span>
          </div>
          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <ol className="space-y-3 list-decimal list-inside">
              <li>
                <span className="font-medium">Create an account only Aurora uses.</span>
                <span className="block text-muted-foreground mt-1">
                  The monitoring tag lets Aurora sign in. It cannot change the broker, and it does not grant access to a vhost. In the management UI, open Admin, then the user, then Set permission. Configure and write are <code>^$</code>, which match nothing, so Aurora cannot create or publish. Read is <code>.*</code>, the whole vhost. Repeat for every vhost Aurora should see. From the broker host, not from Aurora:
                </span>
                <pre className="mt-2 rounded bg-muted/60 border p-2 overflow-auto text-[11px] leading-snug font-mono">{`rabbitmqctl add_user <username> <password>
rabbitmqctl set_user_tags <username> monitoring
rabbitmqctl set_permissions -p / <username> "^$" "^$" ".*"`}</pre>
              </li>
            </ol>
            <div className="mt-4 p-3 bg-orange-50 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-800 rounded">
              <p className="text-xs font-semibold text-orange-900 dark:text-orange-300">What Aurora reads</p>
              <p className="text-xs text-orange-800 dark:text-orange-400 mt-1">
                Queue depth, consumers, dead-letter settings, node memory and disk alarms, connections, and health checks
              </p>
            </div>
          </div>
        </div>
        )}

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            {!addAnother && (
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold">
                2
              </div>
            )}
            <span className="font-semibold">
              {addAnother ? "Add another broker" : "Enter management credentials"}
            </span>
          </div>
          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              {addAnother
                ? "Each broker keeps its own URL, user, and default vhost. Connecting the same management URL again replaces that broker's credentials."
                : "The password is stored in Vault. Only a reference is written to the database."}
            </p>
            <form className="space-y-4" onSubmit={onConnect}>
              <div className="space-y-2">
                <Label htmlFor="rabbitmq-url">Management URL</Label>
                <Input
                  id="rabbitmq-url"
                  type="url"
                  placeholder="https://rabbitmq.internal.example.com:15672"
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  required
                />
              </div>
              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="rabbitmq-username">Username</Label>
                  <Input
                    id="rabbitmq-username"
                    autoComplete="off"
                    placeholder="aurora-monitor"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="rabbitmq-password">Password</Label>
                  <Input
                    id="rabbitmq-password"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="rabbitmq-name">Name (optional)</Label>
                <Input
                  id="rabbitmq-name"
                  autoComplete="off"
                  maxLength={100}
                  placeholder="production"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  How this broker is shown, and a name investigations can use to pick it.
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="rabbitmq-vhost">Default vhost</Label>
                <Input
                  id="rabbitmq-vhost"
                  placeholder="/"
                  value={vhost}
                  onChange={(event) => setVhost(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Used when an investigation does not name a vhost. Leave blank for /.
                </p>
              </div>
              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto">
                  {loading ? "Connecting…" : submitLabel}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
