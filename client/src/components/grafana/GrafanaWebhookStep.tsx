"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { GrafanaStatus } from "@/lib/services/grafana";

interface GrafanaWebhookStepProps {
  status: GrafanaStatus;
  webhookUrl: string;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function GrafanaWebhookStep({
  status,
  webhookUrl,
  copied,
  onCopy,
  onDisconnect,
  loading,
}: GrafanaWebhookStepProps) {
  const router = useRouter();
  const [expandedSubStep, setExpandedSubStep] = useState<number | null>(4);

  const toggleSubStep = (step: number) => {
    setExpandedSubStep(expandedSubStep === step ? null : step);
  };

  return (
    <div className="space-y-6">
      {/* Connection Status Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Grafana Connected</CardTitle>
              <CardDescription>Your Grafana instance is successfully connected</CardDescription>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => router.push("/grafana/alerts")}>
                View Alerts
              </Button>
              <Button variant="destructive" onClick={onDisconnect} disabled={loading}>
                Disconnect
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {status.baseUrl && (
            <div className="flex justify-between py-1 border-b">
              <span className="font-medium">URL:</span>
              <span className="text-muted-foreground">{status.baseUrl}</span>
            </div>
          )}
          {status.org?.name && (
            <div className="flex justify-between py-1 border-b">
              <span className="font-medium">Organization:</span>
              <span className="text-muted-foreground">{status.org.name}</span>
            </div>
          )}
          {status.user?.email && (
            <div className="flex justify-between py-1">
              <span className="font-medium">User:</span>
              <span className="text-muted-foreground">{status.user.email}</span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Webhook Configuration Card */}
      <Card>
        <CardHeader>
          <CardTitle>Step 2: Configure Alert Webhook</CardTitle>
          <CardDescription>Add Aurora as a contact point in your Grafana instance to receive alerts</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Webhook URL */}
          <div>
            <Label className="text-base font-semibold mb-2 block">Your Aurora Webhook URL</Label>
            <div className="flex items-center gap-2 mt-2">
              <code className="flex-1 px-4 py-3 bg-muted rounded text-sm font-mono break-all">
                {webhookUrl}
              </code>
              <Button variant="outline" onClick={onCopy} className="flex-shrink-0">
                {copied ? "Copied!" : "Copy"}
              </Button>
            </div>
          </div>

          {/* Substep 1: Create Contact Point */}
          <div className="border rounded-lg">
            <button
              onClick={() => toggleSubStep(4)}
              className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
            >
              <div className="flex items-center gap-3">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                  1
                </div>
                <span className="font-semibold">Create a Contact Point</span>
              </div>
              <svg
                className={`w-5 h-5 transition-transform ${expandedSubStep === 4 ? "rotate-180" : ""}`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {expandedSubStep === 4 && (
              <div className="p-4 pt-0 space-y-3 text-sm border-t">
                <p className="text-muted-foreground">
                  Contact points define where alert notifications are sent. You can have multiple contact points for different types of alerts.
                </p>
                
                <div className="space-y-2">
                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">1.</span>
                    <div>
                      <p>In your Grafana instance, navigate to:</p>
                      <code className="block px-3 py-2 bg-muted rounded text-xs mt-1">
                        Alerts & IRM → Alerting → Contact points
                      </code>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">2.</span>
                    <p>Click <strong>+ Add contact point</strong></p>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">3.</span>
                    <div>
                      <p>Enter a descriptive <strong>Name</strong> for the contact point</p>
                      <p className="text-xs text-muted-foreground mt-1">
                        Example: "Aurora Platform" or "Aurora Monitoring"
                      </p>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">4.</span>
                    <div>
                      <p>From the <strong>Integration</strong> dropdown, select <strong>Webhook</strong></p>
                    </div>
                  </div>
                </div>

                <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
                  <p className="text-xs font-medium text-blue-900 dark:text-blue-300 mb-1">Tip</p>
                  <p className="text-xs text-blue-800 dark:text-blue-400">
                    You can create different contact points for different teams or severity levels and route them using notification policies.
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Substep 2: Configure Webhook Settings */}
          <div className="border rounded-lg">
            <button
              onClick={() => toggleSubStep(5)}
              className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
            >
              <div className="flex items-center gap-3">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                  2
                </div>
                <span className="font-semibold">Configure Webhook Settings</span>
              </div>
              <svg
                className={`w-5 h-5 transition-transform ${expandedSubStep === 5 ? "rotate-180" : ""}`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {expandedSubStep === 5 && (
              <div className="p-4 pt-0 space-y-3 text-sm border-t">
                <p className="text-muted-foreground">
                  Configure the webhook to send alerts to Aurora's endpoint.
                </p>
                
                <div className="space-y-2">
                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">1.</span>
                    <div className="flex-1">
                      <p className="mb-2">In the <strong>URL</strong> field, paste your Aurora webhook URL:</p>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 px-3 py-2 bg-muted rounded text-xs break-all">
                          {webhookUrl}
                        </code>
                        <button
                          onClick={onCopy}
                          className="p-2 hover:bg-muted rounded"
                          title="Copy URL"
                        >
                          {copied ? (
                            <svg className="w-4 h-4 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          ) : (
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                            </svg>
                          )}
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">2.</span>
                    <div>
                      <p>Leave the <strong>HTTP Method</strong> as <code className="px-1.5 py-0.5 bg-muted rounded text-xs">POST</code></p>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">3.</span>
                    <div>
                      <p><strong>(Optional)</strong> For enhanced security, enable HMAC signature verification:</p>
                      <ul className="text-xs text-muted-foreground mt-1 ml-4 space-y-1">
                        <li>• Enable "Add HMAC signature header"</li>
                        <li>• Enter a shared secret (store securely!)</li>
                        <li>• Header name: <code className="px-1 py-0.5 bg-muted rounded">X-Grafana-Signature</code></li>
                      </ul>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">4.</span>
                    <div>
                      <p>Click <strong>Test</strong> to verify the connection</p>
                      <p className="text-xs text-muted-foreground mt-1">
                        You should see a test alert appear in Aurora within a few seconds
                      </p>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">5.</span>
                    <p>Click <strong>Save contact point</strong></p>
                  </div>
                </div>

                <div className="mt-4 p-3 bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-800 rounded">
                  <p className="text-xs font-medium text-amber-900 dark:text-amber-300 mb-1">Security</p>
                  <p className="text-xs text-amber-800 dark:text-amber-400">
                    Each user gets a unique webhook URL for alert isolation. Never share your webhook URL publicly.
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Substep 3: Add to Notification Policy */}
          <div className="border rounded-lg">
            <button
              onClick={() => toggleSubStep(6)}
              className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
            >
              <div className="flex items-center gap-3">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
                  3
                </div>
                <span className="font-semibold">Add to Notification Policy</span>
              </div>
              <svg
                className={`w-5 h-5 transition-transform ${expandedSubStep === 6 ? "rotate-180" : ""}`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {expandedSubStep === 6 && (
              <div className="p-4 pt-0 space-y-3 text-sm border-t">
                <p className="text-muted-foreground">
                  Notification policies determine which contact points receive alerts based on label matchers and routing rules.
                </p>
                
                <div className="space-y-2">
                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">1.</span>
                    <div>
                      <p>Navigate to:</p>
                      <code className="block px-3 py-2 bg-muted rounded text-xs mt-1">
                        Alerts & IRM → Alerting → Notification policies
                      </code>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">2.</span>
                    <div>
                      <p>Choose one of the following options:</p>
                      <ul className="text-xs text-muted-foreground mt-1 ml-4 space-y-1">
                        <li>• <strong>Option A:</strong> Edit the default policy to include your Aurora contact point</li>
                        <li>• <strong>Option B:</strong> Create a new nested policy with specific label matchers</li>
                      </ul>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">3.</span>
                    <div>
                      <p>In the policy configuration, add your Aurora contact point to the list of contact points</p>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">4.</span>
                    <div>
                      <p><strong>(Optional)</strong> Configure label matchers to route specific alerts to Aurora</p>
                      <p className="text-xs text-muted-foreground mt-1">
                        Example: <code className="px-1 py-0.5 bg-muted rounded">severity=critical</code>
                      </p>
                    </div>
                  </div>

                  <div className="flex items-start gap-2">
                    <span className="text-muted-foreground mt-0.5">5.</span>
                    <p>Click <strong>Save policy</strong></p>
                  </div>
                </div>

                <div className="mt-4 p-3 bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 rounded">
                  <p className="text-xs font-medium text-green-900 dark:text-green-300 mb-1">You're all set!</p>
                  <p className="text-xs text-green-800 dark:text-green-400">
                    Aurora will now receive alerts from Grafana. Visit the <button onClick={() => router.push("/grafana/alerts")} className="underline font-medium">alerts page</button> to view incoming notifications.
                  </p>
                </div>

                <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
                  <p className="text-xs font-medium text-blue-900 dark:text-blue-300 mb-1">Learn More</p>
                  <a
                    href="https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
                  >
                    View Grafana Webhook Documentation
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                    </svg>
                  </a>
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
