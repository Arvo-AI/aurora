"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Eye, EyeOff } from "lucide-react";

interface OpsGenieConnectionStepProps {
  apiKey: string;
  setApiKey: (value: string) => void;
  region: string;
  setRegion: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

const REGION_OPTIONS = [
  { value: "us", label: "US" },
  { value: "eu", label: "EU" },
];

export function OpsGenieConnectionStep({
  apiKey,
  setApiKey,
  region,
  setRegion,
  loading,
  onConnect,
}: OpsGenieConnectionStepProps) {
  const [showApiKey, setShowApiKey] = useState(false);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Connect Your OpsGenie Account</CardTitle>
        <CardDescription>Use an OpsGenie API key to authorise Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-purple-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">How to get your API key</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <ol className="space-y-2 list-decimal list-inside">
              <li>Log in to OpsGenie</li>
              <li>Go to <strong>Settings &rarr; Integrations</strong></li>
              <li>Search for &lsquo;API&rsquo; and select <strong>API Integration</strong></li>
              <li>Click <strong>Add</strong> to create a new integration</li>
              <li>Copy the <strong>API Key</strong></li>
            </ol>
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-950/20 border border-purple-200 dark:border-purple-800 rounded">
              <p className="text-xs font-semibold text-purple-900 dark:text-purple-300">Required permissions</p>
              <p className="text-xs text-purple-800 dark:text-purple-400 mt-1">The API key needs read access to alerts, incidents, services, schedules, and teams.</p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-purple-600 text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter Credentials &amp; Region</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your key securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="opsgenie-api-key">API Key (GenieKey)</Label>
                  <div className="relative">
                    <Input
                      id="opsgenie-api-key"
                      type={showApiKey ? "text" : "password"}
                      placeholder="Paste your OpsGenie API key"
                      value={apiKey}
                      onChange={(event) => setApiKey(event.target.value)}
                      required
                      className="pr-10"
                    />
                    <button
                      type="button"
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                      onClick={() => setShowApiKey(!showApiKey)}
                      tabIndex={-1}
                    >
                      {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>Region</Label>
                  <div className="flex gap-2">
                    {REGION_OPTIONS.map((opt) => (
                      <button
                        type="button"
                        key={opt.value}
                        onClick={() => setRegion(opt.value)}
                        className={`flex-1 px-4 py-2 rounded border text-sm font-medium transition-colors ${
                          region === opt.value
                            ? 'border-purple-600 bg-purple-600 text-white hover:bg-purple-600/90'
                            : 'border-muted-foreground/30 text-muted-foreground hover:border-purple-400 hover:text-foreground'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-muted-foreground">Select the region where your OpsGenie instance is hosted.</p>
                </div>
              </div>

              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto">
                  {loading ? "Connecting\u2026" : "Connect OpsGenie"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
