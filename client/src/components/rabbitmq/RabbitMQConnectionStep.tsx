"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";

interface RabbitMQConnectionStepProps {
  url: string;
  setUrl: (value: string) => void;
  username: string;
  setUsername: (value: string) => void;
  password: string;
  setPassword: (value: string) => void;
  vhost: string;
  setVhost: (value: string) => void;
  skipTls: boolean;
  setSkipTls: (value: boolean) => void;
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
  skipTls,
  setSkipTls,
  loading,
  onConnect,
}: Readonly<RabbitMQConnectionStepProps>) {
  return (
    <Card>
      <CardContent className="space-y-6 pt-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Prerequisites</span>
          </div>
          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora reads the RabbitMQ management HTTP API. It does not publish, purge, or fetch message bodies.
            </p>
            <ol className="space-y-2 list-decimal list-inside">
              <li>Enable the management plugin and note its URL, usually port 15672.</li>
              <li>Create a user with the <span className="font-medium">monitoring</span> tag. Administrator also works, but monitoring cannot change the broker.</li>
              <li>The URL must be reachable from this Aurora deployment. Private network addresses are allowed.</li>
            </ol>
            <div className="mt-4 p-3 bg-orange-50 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-800 rounded">
              <p className="text-xs font-semibold text-orange-900 dark:text-orange-300">What Aurora reads</p>
              <p className="text-xs text-orange-800 dark:text-orange-400 mt-1">
                Queue depth, consumers, dead-letter settings, node memory and disk alarms, connections, and health checks
              </p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter management credentials</span>
          </div>
          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              The password is stored in Vault. Only a reference is written to the database.
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
              <div className="flex items-start gap-2">
                <Checkbox
                  id="rabbitmq-skip-tls"
                  checked={skipTls}
                  onCheckedChange={(checked) => setSkipTls(checked === true)}
                />
                <div>
                  <Label htmlFor="rabbitmq-skip-tls">Skip TLS certificate verification</Label>
                  <p className="text-xs text-muted-foreground">
                    Only for a private CA. Leave this off when the broker has a public certificate.
                  </p>
                </div>
              </div>
              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto">
                  {loading ? "Connecting…" : "Connect RabbitMQ"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
