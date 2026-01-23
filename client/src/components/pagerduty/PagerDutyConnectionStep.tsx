"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { AlertCircle } from "lucide-react";

interface PagerDutyConnectionStepProps {
  displayName: string;
  setDisplayName: (value: string) => void;
  token: string;
  setToken: (value: string) => void;
  loading: boolean;
  error: string | null;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
  onOAuthConnect?: () => void;
}

export function PagerDutyConnectionStep({
  displayName,
  setDisplayName,
  token,
  setToken,
  loading,
  error,
  onConnect,
  onOAuthConnect,
}: PagerDutyConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Authentication</CardTitle>
        <CardDescription>
          Connect via OAuth or provide an API token
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <Alert variant="destructive" className="mb-6">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {onOAuthConnect && (
          <div className="space-y-4 mb-6">
            <Button
              type="button"
              onClick={onOAuthConnect}
              disabled={loading}
              className="w-full bg-[#25c151] hover:bg-[#1ea842] text-white"
            >
              Connect with OAuth
            </Button>
            
            <div className="relative flex items-center gap-2 py-2">
              <div className="flex-1 border-t border-border"></div>
              <span className="text-xs text-muted-foreground px-2">OR</span>
              <div className="flex-1 border-t border-border"></div>
            </div>
          </div>
        )}

        <form onSubmit={onConnect} className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="pagerduty-display-name">
              Display Name <span className="text-muted-foreground font-normal">(optional)</span>
            </Label>
            <Input
              id="pagerduty-display-name"
              placeholder="PagerDuty"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              disabled={loading}
            />
          </div>

          <div className="space-y-3">
            <Label htmlFor="pagerduty-token">API Token</Label>
            <Input
              id="pagerduty-token"
              type="password"
              placeholder="Enter your PagerDuty API token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              required
              disabled={loading}
            />
            
            <div className="p-4 bg-muted/50 border rounded-lg">
              <p className="text-sm font-medium mb-2">How to get your API token</p>
              <ol className="text-xs text-muted-foreground space-y-1.5 ml-4 list-decimal">
                <li>Log in to your PagerDuty account</li>
                <li>Navigate to <strong>User Icon → My Profile → User Settings</strong></li>
                <li>Click <strong>Create API User Token</strong> or <strong>API Access Keys</strong></li>
                <li>Copy the token and paste it above</li>
              </ol>
              <a
                href="https://support.pagerduty.com/docs/api-access-keys"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 mt-3 text-xs font-medium text-primary hover:underline"
              >
                View detailed guide →
              </a>
            </div>
          </div>

          <Button 
            type="submit" 
            disabled={loading || !token} 
            className="w-full"
            variant="outline"
          >
            {loading ? "Validating…" : "Connect with API Token"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

