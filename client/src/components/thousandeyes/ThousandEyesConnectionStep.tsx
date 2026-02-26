"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface ThousandEyesConnectionStepProps {
  apiToken: string;
  setApiToken: (value: string) => void;
  accountGroupId: string;
  setAccountGroupId: (value: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function ThousandEyesConnectionStep({
  apiToken,
  setApiToken,
  accountGroupId,
  setAccountGroupId,
  loading,
  onConnect,
}: ThousandEyesConnectionStepProps) {
  return (
    <Card>
      <CardContent className="space-y-6 pt-6">
        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
              1
            </div>
            <span className="font-semibold">Prerequisites</span>
          </div>

          <div className="p-4 pt-0 space-y-3 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora connects to ThousandEyes using an OAuth Bearer token from the ThousandEyes API v7.
            </p>
            <ol className="space-y-2 list-decimal list-inside">
              <li>Log in to ThousandEyes at <strong>app.thousandeyes.com</strong>.</li>
              <li>Navigate to <strong>Account Settings &gt; Users and Roles &gt; User API Tokens</strong>.</li>
              <li>Create a new OAuth Bearer Token with read permissions.</li>
              <li>Copy the token (it will only be shown once).</li>
            </ol>
            <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
              <p className="text-xs font-semibold text-blue-900 dark:text-blue-300">What Aurora accesses</p>
              <p className="text-xs text-blue-800 dark:text-blue-400 mt-1">
                Tests &amp; results, alerts, agents, Internet Insights (network &amp; app outages), path visualization, and BGP data
              </p>
            </div>
          </div>
        </div>

        <div className="border rounded-lg">
          <div className="w-full p-4 flex items-center gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-600 text-white text-sm font-bold">
              2
            </div>
            <span className="font-semibold">Enter ThousandEyes Credentials</span>
          </div>

          <div className="p-4 pt-0 space-y-4 text-sm border-t">
            <p className="text-muted-foreground">
              Aurora stores your credentials securely using Vault. Only encrypted references are persisted in the database.
            </p>

            <form className="space-y-4" onSubmit={onConnect}>
              <div className="space-y-2">
                <Label htmlFor="te-token">Bearer Token</Label>
                <Input
                  id="te-token"
                  type="password"
                  placeholder="Enter your OAuth Bearer Token"
                  value={apiToken}
                  onChange={(event) => setApiToken(event.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  The OAuth Bearer Token from ThousandEyes Account Settings &gt; User API Tokens
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="te-account-group">Account Group ID (optional)</Label>
                <Input
                  id="te-account-group"
                  type="text"
                  placeholder="e.g. 12345"
                  value={accountGroupId}
                  onChange={(event) => setAccountGroupId(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Only needed if your organization has multiple account groups and you want to target a specific one
                </p>
              </div>

              <div className="pt-2">
                <Button type="submit" disabled={loading} className="w-full md:w-auto">
                  {loading ? "Connecting\u2026" : "Connect ThousandEyes"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
