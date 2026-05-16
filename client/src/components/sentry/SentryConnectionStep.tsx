"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Copy, Check } from "lucide-react";
import { SENTRY_PURPLE } from "./constants";

interface SentryConnectionStepProps {
  authToken: string;
  setAuthToken: (value: string) => void;
  orgSlug: string;
  setOrgSlug: (value: string) => void;
  region: string;
  setRegion: (value: string) => void;
  webhookSecret: string;
  setWebhookSecret: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
  webhookUrl: string | null;
  copied: boolean;
  onCopyWebhook: () => void;
}

const REGION_HINTS = [
  { value: "us", label: "US (sentry.io)" },
  { value: "eu", label: "EU (de.sentry.io)" },
];

export function SentryConnectionStep({
  authToken,
  setAuthToken,
  orgSlug,
  setOrgSlug,
  region,
  setRegion,
  webhookSecret,
  setWebhookSecret,
  loading,
  onConnect,
  webhookUrl,
  copied,
  onCopyWebhook,
}: SentryConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Create a Sentry Internal Integration</CardTitle>
        <CardDescription>Aurora connects via a Sentry Internal Integration auth token. You&apos;ll create the integration in Sentry, then paste the credentials here.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full text-white text-sm font-bold" style={{ backgroundColor: SENTRY_PURPLE }}>
              1
            </div>
            <span className="font-semibold">Create the Internal Integration in Sentry</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <ol className="space-y-2 list-decimal list-inside">
              <li>In Sentry, go to <strong>Settings &rarr; Custom Integrations</strong> (under <em>Developer Settings</em>).</li>
              <li>Click <strong>Create New Integration</strong> and choose <strong>Internal Integration</strong>.</li>
              <li>Name it <code>Aurora</code> and paste the webhook URL below into the <strong>Webhook URL</strong> field.</li>
              <li>Under <strong>Permissions</strong>, grant read access to: <strong>Issue &amp; Event</strong>, <strong>Project</strong>, <strong>Organization</strong>.</li>
              <li>Under <strong>Webhooks</strong>, subscribe to: <code>issue</code> and <code>error</code> (Business/Enterprise plans).</li>
              <li>Save. Under <strong>Credentials</strong>, copy the <strong>Client Secret</strong>.</li>
              <li>Scroll to <strong>Tokens</strong> and click <strong>Create New Token</strong>. Sentry doesn&apos;t auto-generate one — you have to create it yourself. Copy the token (starts with <code>sntrys_</code>) before leaving the page; it&apos;s shown once.</li>
            </ol>

            <div className="space-y-2 pt-2">
              <Label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Aurora Webhook URL</Label>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-muted px-3 py-2 rounded text-xs break-all">
                  {webhookUrl ?? "Loading…"}
                </code>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={onCopyWebhook}
                  disabled={!webhookUrl}
                  className="shrink-0"
                  aria-label={copied ? "Webhook URL copied" : "Copy webhook URL"}
                >
                  {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">Paste this into Sentry&apos;s <strong>Webhook URL</strong> field before subscribing to webhook resources.</p>
            </div>

            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-950/20 border border-purple-200 dark:border-purple-800 rounded">
              <p className="text-xs font-semibold text-purple-900 dark:text-purple-300">Read-only is enough</p>
              <p className="text-xs text-purple-800 dark:text-purple-400 mt-1">Aurora never writes to Sentry during RCA. Grant only read permissions; revoke the integration in Sentry at any time to immediately cut Aurora&apos;s access.</p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full text-white text-sm font-bold" style={{ backgroundColor: SENTRY_PURPLE }}>
              2
            </div>
            <span className="font-semibold">Enter Credentials</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your auth token and client secret securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="sentry-org-slug">Organization Slug</Label>
                  <Input
                    id="sentry-org-slug"
                    placeholder="acme-co"
                    value={orgSlug}
                    onChange={(e) => setOrgSlug(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">The slug in your Sentry URL (e.g. <code>acme-co</code>, not <em>Acme Co</em>)</p>
                </div>
                <div className="space-y-2">
                  <Label id="sentry-region-label">Region</Label>
                  <div className="flex gap-2" role="group" aria-labelledby="sentry-region-label">
                    {REGION_HINTS.map(hint => (
                      <button
                        type="button"
                        key={hint.value}
                        onClick={() => setRegion(hint.value)}
                        aria-pressed={region === hint.value}
                        className={`px-4 py-2 rounded border transition-colors font-medium text-sm ${
                          region === hint.value
                            ? 'text-white hover:opacity-90'
                            : 'border-muted-foreground/30 text-muted-foreground hover:text-foreground'
                        }`}
                        style={region === hint.value ? { backgroundColor: SENTRY_PURPLE, borderColor: SENTRY_PURPLE } : undefined}
                      >
                        {hint.label}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-muted-foreground">Pick the host your Sentry org is on</p>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="sentry-auth-token">Auth Token</Label>
                <Input
                  id="sentry-auth-token"
                  type="password"
                  placeholder="sntrys_..."
                  value={authToken}
                  onChange={(e) => setAuthToken(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">Internal Integration auth token (shown once when you save the integration)</p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="sentry-client-secret">Client Secret</Label>
                <Input
                  id="sentry-client-secret"
                  type="password"
                  placeholder="Required to verify incoming webhooks"
                  value={webhookSecret}
                  onChange={(e) => setWebhookSecret(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Shown by Sentry under <strong>Credentials</strong> when you save the Internal Integration. Aurora uses it to verify HMAC-SHA256 signatures on every webhook — without it, webhook delivery is rejected.
                </p>
              </div>

              <div className="pt-2">
                <Button
                  type="submit"
                  disabled={loading}
                  className="w-full md:w-auto text-white hover:opacity-90"
                  style={{ backgroundColor: SENTRY_PURPLE }}
                >
                  {loading ? "Connecting..." : "Connect Sentry"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
