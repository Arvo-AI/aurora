"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface GrafanaConnectionStepProps {
  baseUrl: string;
  setBaseUrl: (url: string) => void;
  apiToken: string;
  setApiToken: (token: string) => void;
  stackSlug: string;
  setStackSlug: (slug: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function GrafanaConnectionStep({
  baseUrl,
  setBaseUrl,
  apiToken,
  setApiToken,
  stackSlug,
  setStackSlug,
  loading,
  onConnect,
}: GrafanaConnectionStepProps) {
  const [expandedSubStep, setExpandedSubStep] = useState<number | null>(1);

  const toggleSubStep = (step: number) => {
    setExpandedSubStep(expandedSubStep === step ? null : step);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Connect Your Grafana Instance</CardTitle>
        <CardDescription>Create a service account in Grafana and connect it to Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Substep 1: Create Service Account */}
        <div className="border rounded-lg">
          <button
            onClick={() => toggleSubStep(1)}
            className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                1
              </div>
              <span className="font-semibold">Create a Grafana Service Account</span>
            </div>
            <svg
              className={`w-5 h-5 transition-transform ${expandedSubStep === 1 ? "rotate-180" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {expandedSubStep === 1 && (
            <div className="p-4 pt-0 space-y-3 text-sm border-t">
              <p className="text-muted-foreground">
                Service accounts allow applications to authenticate with the Grafana API without using a user account.
              </p>
              
              <div className="space-y-2">
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">1.</span>
                  <div>
                    <p>Sign in to your Grafana instance and navigate to:</p>
                    <code className="block px-3 py-2 bg-muted rounded text-xs mt-1">
                      Administration → Users and access → Service accounts
                    </code>
                  </div>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">2.</span>
                  <p>Click <strong>Add service account</strong></p>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">3.</span>
                  <div>
                    <p>Enter a descriptive <strong>Display name</strong> (e.g., "Aurora Integration")</p>
                    <p className="text-xs text-muted-foreground mt-1">
                      The display name must be unique and determines the service account ID
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">4.</span>
                  <p>Assign the <strong>Admin</strong> role to the service account</p>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">5.</span>
                  <p>Click <strong>Create</strong></p>
                </div>
              </div>

              <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
                <p className="text-xs font-medium text-blue-900 dark:text-blue-300 mb-1">Learn More</p>
                <a
                  href="https://grafana.com/docs/grafana/latest/administration/service-accounts/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
                >
                  View Grafana Service Accounts Documentation
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                  </svg>
                </a>
              </div>
            </div>
          )}
        </div>

        {/* Substep 2: Generate API Token */}
        <div className="border rounded-lg">
          <button
            onClick={() => toggleSubStep(2)}
            className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                2
              </div>
              <span className="font-semibold">Generate an API Token</span>
            </div>
            <svg
              className={`w-5 h-5 transition-transform ${expandedSubStep === 2 ? "rotate-180" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {expandedSubStep === 2 && (
            <div className="p-4 pt-0 space-y-3 text-sm border-t">
              <p className="text-muted-foreground">
                Create a secure token for API access to your Grafana instance.
              </p>
              
              <div className="space-y-2">
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">1.</span>
                  <p>Click on the service account you just created</p>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">2.</span>
                  <p>Click <strong>Add service account token</strong></p>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">3.</span>
                  <div>
                    <p>Enter a descriptive <strong>Token name</strong> (e.g., "Aurora API Access")</p>
                  </div>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">4.</span>
                  <div>
                    <p><strong>(Recommended)</strong> Set an expiration date</p>
                    <p className="text-xs text-muted-foreground mt-1">
                      Choose a date that balances security with operational needs. Tokens without expiration dates never expire.
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">5.</span>
                  <p>Click <strong>Generate token</strong></p>
                </div>

                <div className="flex items-start gap-2">
                  <span className="text-red-600 mt-0.5">️</span>
                  <div>
                    <p className="text-red-600 font-medium">Important: Copy the token immediately!</p>
                    <p className="text-xs text-red-600 mt-1">
                      You won't be able to see it again after closing the dialog.
                    </p>
                  </div>
                </div>
              </div>

              <div className="mt-4 p-3 bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-800 rounded">
                <p className="text-xs font-medium text-amber-900 dark:text-amber-300 mb-1">Security</p>
                <p className="text-xs text-amber-800 dark:text-amber-400">
                  Aurora encrypts and stores your API token securely using Vault. The token is never exposed to your browser after submission.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Substep 3: Connect to Aurora */}
        <div className="border rounded-lg">
          <button
            onClick={() => toggleSubStep(3)}
            className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                3
              </div>
              <span className="font-semibold">Connect to Aurora</span>
            </div>
            <svg
              className={`w-5 h-5 transition-transform ${expandedSubStep === 3 ? "rotate-180" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {expandedSubStep === 3 && (
            <div className="p-4 pt-0 border-t">
              <p className="text-sm text-muted-foreground mb-4">
                Enter your Grafana connection details below to complete the integration.
              </p>
            </div>
          )}
        </div>

        <form onSubmit={onConnect} className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="grafana-base-url">Grafana Base URL *</Label>
            <Input
              id="grafana-base-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-instance.grafana.net"
              required
              disabled={loading}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="grafana-stack-slug">Stack Slug (Optional)</Label>
            <Input
              id="grafana-stack-slug"
              value={stackSlug}
              onChange={(e) => setStackSlug(e.target.value)}
              placeholder="my-stack"
              disabled={loading}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="grafana-token">Service Account Token *</Label>
            <textarea
              id="grafana-token"
              className="min-h-[100px] rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="glsa_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              required
              disabled={loading}
            />
            <p className="text-xs text-muted-foreground">
              Token is encrypted and stored securely in Vault
            </p>
          </div>

          <div className="flex items-center justify-end pt-4">
            <Button type="submit" disabled={loading}>
              {loading ? "Connecting..." : "Connect Grafana"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
