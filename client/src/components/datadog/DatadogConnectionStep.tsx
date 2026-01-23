"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface DatadogConnectionStepProps {
  apiKey: string;
  setApiKey: (value: string) => void;
  appKey: string;
  setAppKey: (value: string) => void;
  site: string;
  setSite: (value: string) => void;
  serviceAccountName: string;
  setServiceAccountName: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

const SITE_HINTS = [
  { value: "datadoghq.com", label: "US1" },
  { value: "us3.datadoghq.com", label: "US3" },
  { value: "us5.datadoghq.com", label: "US5" },
  { value: "datadoghq.eu", label: "EU" },
  { value: "ap1.datadoghq.com", label: "AP1" },
  { value: "ap2.datadoghq.com", label: "AP2" },
  { value: "ddog-gov.com", label: "US Gov" },
];

export function DatadogConnectionStep({
  apiKey,
  setApiKey,
  appKey,
  setAppKey,
  site,
  setSite,
  serviceAccountName,
  setServiceAccountName,
  loading,
  onConnect,
}: DatadogConnectionStepProps) {
  const normalizeSite = (value: string): string => {
    return value
      .trim()
      .toLowerCase()
      .replace(/^https?:\/\//, '')
      .replace(/\/+$/, '');
  };

  const activeSite = normalizeSite(site || '');

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Connect Your Datadog Organization</CardTitle>
        <CardDescription>Use a Datadog service account with API + application keys to authorise Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-purple-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Create a Datadog Service Account Key Pair</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <p className="text-muted-foreground">
              Service accounts (recommended) keep Datadog integrations isolated from personal accounts. They require both an API key and an application key.
            </p>
            <ol className="space-y-2 list-decimal list-inside">
              <li>Open the bottom-left <strong>Accounts</strong> menu in Datadog and switch to the desired organization if needed.</li>
              <li>Under <strong>Organization Settings → Service Accounts</strong>, create (or identify) a service account dedicated to Aurora.</li>
              <li>Still in the Accounts menu, create a new <strong>API Key</strong> (Organization Settings → API Keys) for that service account.</li>
              <li>Generate an <strong>Application Key</strong> (Organization Settings → Application Keys) that is linked to the same service account or team user with read access to logs, metrics, monitors, and events.</li>
              <li>Record the service account name or email so Aurora can display who owns the integration.</li>
            </ol>
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-950/20 border border-purple-200 dark:border-purple-800 rounded">
              <p className="text-xs font-semibold text-purple-900 dark:text-purple-300">Minimum permissions</p>
              <p className="text-xs text-purple-800 dark:text-purple-400 mt-1">logs_read · metrics_read · monitors_read · events_read</p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-purple-600 text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter Credentials &amp; Datadog Site</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your keys securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
                <div className="grid md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="datadog-site">Datadog Site</Label>
                    <Input
                      id="datadog-site"
                      placeholder="datadoghq.com"
                      value={site}
                      onChange={(event) => setSite(event.target.value)}
                      required
                    />
                    <p className="text-xs text-muted-foreground">Use your region host (e.g., datadoghq.com, datadoghq.eu, us3.datadoghq.com)</p>
                    <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                      {SITE_HINTS.map(hint => (
                        <button
                          type="button"
                          key={hint.value}
                          onClick={() => setSite(hint.value)}
                          className={`px-2 py-1 rounded border transition-colors ${
                            normalizeSite(hint.value) === activeSite
                              ? 'border-purple-600 bg-purple-600 text-white hover:bg-purple-600/90'
                              : 'border-muted-foreground/30 text-muted-foreground hover:border-purple-400 hover:text-foreground'
                          }`}
                        >
                          {hint.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="datadog-service-account">Service Account (optional)</Label>
                    <Input
                      id="datadog-service-account"
                      placeholder="aurora-integration@yourcompany.com"
                      value={serviceAccountName}
                      onChange={(event) => setServiceAccountName(event.target.value)}
                    />
                    <p className="text-xs text-muted-foreground">Shown in Aurora to help your team identify who owns the integration.</p>
                  </div>
                </div>

                <div className="grid md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="datadog-api-key">API Key</Label>
                    <Input
                      id="datadog-api-key"
                      type="password"
                      placeholder="Copy your API key"
                      value={apiKey}
                      onChange={(event) => setApiKey(event.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="datadog-app-key">Application Key</Label>
                    <Input
                      id="datadog-app-key"
                      type="password"
                      placeholder="Copy your application key"
                      value={appKey}
                      onChange={(event) => setAppKey(event.target.value)}
                      required
                    />
                  </div>
                </div>

                <div className="pt-2">
                  <Button type="submit" disabled={loading} className="w-full md:w-auto">
                    {loading ? "Connecting…" : "Connect Datadog"}
                  </Button>
                </div>
              </form>
            </div>
        </div>
      </CardContent>
    </Card>
  );
}
