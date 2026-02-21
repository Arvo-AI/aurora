"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface CorootConnectionStepProps {
  url: string;
  setUrl: (value: string) => void;
  email: string;
  setEmail: (value: string) => void;
  password: string;
  setPassword: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function CorootConnectionStep({
  url,
  setUrl,
  email,
  setEmail,
  password,
  setPassword,
  loading,
  onConnect,
}: CorootConnectionStepProps) {
  return (
    <Card>
      <CardContent className="space-y-6 pt-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Prerequisites</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora connects to your Coroot instance via its HTTP API using session-cookie
              authentication. No direct database access or API keys are needed.
            </p>
            <ol className="space-y-2 list-decimal list-inside">
              <li>Ensure your Coroot instance is accessible from this Aurora deployment.</li>
              <li>Use an existing Coroot account (email + password). A dedicated service account is recommended.</li>
              <li>The account needs access to the project(s) you want Aurora to monitor.</li>
            </ol>
            <div className="mt-4 p-3 bg-emerald-50 dark:bg-emerald-950/20 border border-emerald-200 dark:border-emerald-800 rounded">
              <p className="text-xs font-semibold text-emerald-900 dark:text-emerald-300">What Aurora accesses</p>
              <p className="text-xs text-emerald-800 dark:text-emerald-400 mt-1">
                Metrics (PromQL), logs, traces, incidents + RCA, service maps, profiling, deployments, costs, and risks
              </p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-600 text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter Coroot Credentials</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your credentials securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
              <div className="space-y-2">
                <Label htmlFor="coroot-url">Coroot URL</Label>
                <Input
                  id="coroot-url"
                  type="url"
                  placeholder="https://coroot.example.com"
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  The full URL to your Coroot instance (e.g., https://coroot.example.com or http://localhost:8080)
                </p>
              </div>

              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="coroot-email">Email</Label>
                  <Input
                    id="coroot-email"
                    type="email"
                    placeholder="admin@example.com"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="coroot-password">Password</Label>
                  <Input
                    id="coroot-password"
                    type="password"
                    placeholder="Enter password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                  />
                </div>
              </div>

              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto">
                  {loading ? "Connecting\u2026" : "Connect Coroot"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
