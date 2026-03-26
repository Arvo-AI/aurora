"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface NewRelicConnectionStepProps {
  apiKey: string;
  setApiKey: (value: string) => void;
  accountId: string;
  setAccountId: (value: string) => void;
  region: string;
  setRegion: (value: string) => void;
  licenseKey: string;
  setLicenseKey: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

const REGION_HINTS = [
  { value: "us", label: "US" },
  { value: "eu", label: "EU" },
];

export function NewRelicConnectionStep({
  apiKey,
  setApiKey,
  accountId,
  setAccountId,
  region,
  setRegion,
  licenseKey,
  setLicenseKey,
  loading,
  onConnect,
}: NewRelicConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Connect Your New Relic Account</CardTitle>
        <CardDescription>Provide your NerdGraph User API key and Account ID to authorise Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-[#00AC69] text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Create a New Relic User API Key</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora uses the NerdGraph GraphQL API (User API Key) to query metrics, logs, traces, and incidents from your account.
            </p>
            <ol className="space-y-2 list-decimal list-inside">
              <li>Log in to <strong>one.newrelic.com</strong> and go to <strong>Administration &rarr; API keys</strong> (or visit <a href="https://one.newrelic.com/admin-portal/api-keys/" target="_blank" rel="noopener noreferrer" className="underline">one.newrelic.com/admin-portal/api-keys</a>).</li>
              <li>Click <strong>Create a key</strong> and select <strong>User</strong> as the key type.</li>
              <li>Name the key (e.g., &ldquo;Aurora Integration&rdquo;) and save it.</li>
              <li>Copy the key &mdash; it starts with <code>NRAK-</code>.</li>
              <li>Find your <strong>Account ID</strong> in the account dropdown or on the API keys page.</li>
            </ol>
            <div className="mt-4 p-3 bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 rounded">
              <p className="text-xs font-semibold text-green-900 dark:text-green-300">Required permissions</p>
              <p className="text-xs text-green-800 dark:text-green-400 mt-1">The User key inherits the permissions of the user who created it. Ensure the user has read access to APM, Infrastructure, Logs, and Alerts.</p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-[#00AC69] text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter Credentials</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your keys securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="nr-account-id">Account ID</Label>
                  <Input
                    id="nr-account-id"
                    placeholder="1234567"
                    value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">Found in the account dropdown or on the API keys page</p>
                </div>
                <div className="space-y-2">
                  <Label id="nr-region-label">Data Center Region</Label>
                  <div className="flex gap-2" role="group" aria-labelledby="nr-region-label">
                    {REGION_HINTS.map(hint => (
                      <button
                        type="button"
                        key={hint.value}
                        onClick={() => setRegion(hint.value)}
                        aria-pressed={region === hint.value}
                        className={`px-4 py-2 rounded border transition-colors font-medium ${
                          region === hint.value
                            ? 'border-[#00AC69] bg-[#00AC69] text-white hover:bg-[#00AC69]/90'
                            : 'border-muted-foreground/30 text-muted-foreground hover:border-[#00AC69] hover:text-foreground'
                        }`}
                      >
                        {hint.label}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-muted-foreground">US = api.newrelic.com, EU = api.eu.newrelic.com</p>
                </div>
              </div>

              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="nr-api-key">User API Key</Label>
                  <Input
                    id="nr-api-key"
                    type="password"
                    placeholder="NRAK-..."
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">Used for querying NerdGraph (read access)</p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="nr-license-key">License Key (optional)</Label>
                  <Input
                    id="nr-license-key"
                    type="password"
                    placeholder="Optional — for future write-back features"
                    value={licenseKey}
                    onChange={(e) => setLicenseKey(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">40-character ingest key, only needed if Aurora writes annotations back</p>
                </div>
              </div>

              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto bg-[#00AC69] text-white hover:bg-[#00AC69]/90">
                  {loading ? "Connecting..." : "Connect New Relic"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
