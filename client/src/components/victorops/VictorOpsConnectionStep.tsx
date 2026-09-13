"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { AlertCircle } from "lucide-react";

interface VictorOpsConnectionStepProps {
  readonly displayName: string;
  readonly setDisplayName: (value: string) => void;
  readonly apiId: string;
  readonly setApiId: (value: string) => void;
  readonly apiKey: string;
  readonly setApiKey: (value: string) => void;
  readonly loading: boolean;
  readonly error: string | null;
  readonly onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function VictorOpsConnectionStep({
  displayName,
  setDisplayName,
  apiId,
  setApiId,
  apiKey,
  setApiKey,
  loading,
  error,
  onConnect,
}: VictorOpsConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Authentication</CardTitle>
        <CardDescription>
          Connect with your Splunk On-Call API ID and API Key
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <Alert variant="destructive" className="mb-6">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <form onSubmit={onConnect} className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="victorops-display-name">
              Display Name{" "}
              <span className="text-muted-foreground font-normal">
                (optional)
              </span>
            </Label>
            <Input
              id="victorops-display-name"
              placeholder="Splunk On-Call"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              disabled={loading}
            />
          </div>

          <div className="space-y-3">
            <Label htmlFor="victorops-api-id">API ID</Label>
            <Input
              id="victorops-api-id"
              placeholder="Enter your Splunk On-Call API ID"
              value={apiId}
              onChange={(e) => setApiId(e.target.value)}
              required
              disabled={loading}
            />
          </div>

          <div className="space-y-3">
            <Label htmlFor="victorops-api-key">API Key</Label>
            <Input
              id="victorops-api-key"
              type="password"
              placeholder="Enter your Splunk On-Call API Key"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              required
              disabled={loading}
            />

            <div className="p-4 bg-muted/50 border rounded-lg">
              <p className="text-sm font-medium mb-2">
                How to get your API credentials
              </p>
              <ol className="text-xs text-muted-foreground space-y-1.5 ml-4 list-decimal">
                <li>Log in to your Splunk On-Call portal</li>
                <li>
                  Go to{" "}
                  <strong className="text-foreground">
                    Integrations → API
                  </strong>
                </li>
                <li>
                  Copy your{" "}
                  <strong className="text-foreground">API ID</strong> and click{" "}
                  <strong className="text-foreground">Create API Key</strong>
                </li>
                <li>Paste both values above</li>
              </ol>
              <a
                href="https://help.victorops.com/knowledge-base/api/"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 mt-3 text-xs font-medium text-primary hover:underline"
              >
                View API documentation →
              </a>
            </div>
          </div>

          <Button
            type="submit"
            disabled={loading || !apiId || !apiKey}
            className="w-full"
            variant="outline"
          >
            {loading ? "Validating…" : "Connect Splunk On-Call"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
