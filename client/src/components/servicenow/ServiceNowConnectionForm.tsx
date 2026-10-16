"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import type { ServiceNowAuthType, ServiceNowConnectPayload } from "@/lib/services/servicenow";

interface ServiceNowConnectionFormProps {
  readonly onConnect: (payload: ServiceNowConnectPayload) => Promise<void>;
  readonly loading: boolean;
}

const INSTANCE_URL_PATTERN = /^https:\/\/[a-z0-9-]+\.service-now\.com\/?$/i;

export function ServiceNowConnectionForm({ onConnect, loading }: ServiceNowConnectionFormProps) {
  const [instanceUrl, setInstanceUrl] = useState("");
  const [authType, setAuthType] = useState<ServiceNowAuthType>("basic");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [displayName, setDisplayName] = useState("");

  const trimmedUrl = instanceUrl.trim();
  const urlValid = INSTANCE_URL_PATTERN.test(trimmedUrl);
  const credentialsFilled =
    authType === "basic" ? Boolean(username.trim() && password) : Boolean(clientId.trim() && clientSecret);
  const canSubmit = urlValid && credentialsFilled && !loading;

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const payload: ServiceNowConnectPayload = {
      instanceUrl: trimmedUrl.replace(/\/$/, ""),
      authType,
      displayName: displayName.trim() || undefined,
      ...(authType === "basic"
        ? { username: username.trim(), password }
        : { clientId: clientId.trim(), clientSecret }),
    };
    try {
      await onConnect(payload);
    } finally {
      setPassword("");
      setClientSecret("");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Connect to ServiceNow</CardTitle>
        <CardDescription>
          Enter your ServiceNow instance URL and the credentials of a service account Aurora can use to read
          incidents and write work notes.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <Label htmlFor="servicenow-instance-url">Instance URL</Label>
            <Input
              id="servicenow-instance-url"
              type="url"
              placeholder="https://your-instance.service-now.com"
              value={instanceUrl}
              onChange={(e) => setInstanceUrl(e.target.value)}
              required
            />
            {trimmedUrl && !urlValid && (
              <p className="text-xs text-red-600 dark:text-red-400">
                Use the form https://&lt;name&gt;.service-now.com
              </p>
            )}
          </div>

          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Authentication method</legend>
            <div className="flex flex-col gap-2 sm:flex-row sm:gap-6">
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="radio"
                  name="servicenow-auth-type"
                  value="basic"
                  checked={authType === "basic"}
                  onChange={() => setAuthType("basic")}
                />
                Basic auth (username and password)
              </label>
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="radio"
                  name="servicenow-auth-type"
                  value="oauth_client_credentials"
                  checked={authType === "oauth_client_credentials"}
                  onChange={() => setAuthType("oauth_client_credentials")}
                />
                OAuth client credentials
              </label>
            </div>
          </fieldset>

          {authType === "basic" ? (
            <>
              <div className="space-y-2">
                <Label htmlFor="servicenow-username">Username</Label>
                <Input
                  id="servicenow-username"
                  autoComplete="off"
                  placeholder="Service account username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="servicenow-password">Password</Label>
                <Input
                  id="servicenow-password"
                  type="password"
                  autoComplete="new-password"
                  placeholder="Service account password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
            </>
          ) : (
            <>
              <div className="space-y-2">
                <Label htmlFor="servicenow-client-id">Client ID</Label>
                <Input
                  id="servicenow-client-id"
                  autoComplete="off"
                  placeholder="OAuth application client ID"
                  value={clientId}
                  onChange={(e) => setClientId(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="servicenow-client-secret">Client Secret</Label>
                <Input
                  id="servicenow-client-secret"
                  type="password"
                  autoComplete="new-password"
                  placeholder="OAuth application client secret"
                  value={clientSecret}
                  onChange={(e) => setClientSecret(e.target.value)}
                  required
                />
              </div>
            </>
          )}

          <div className="space-y-2">
            <Label htmlFor="servicenow-display-name">Display name (optional)</Label>
            <Input
              id="servicenow-display-name"
              placeholder="e.g. Production ServiceNow"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </div>

          <p className="text-xs text-muted-foreground">
            The account needs read access to the incident table and permission to add work notes. Credentials are
            stored securely in Vault.
          </p>

          <Button type="submit" className="w-full" disabled={!canSubmit}>
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Connecting...
              </>
            ) : (
              "Connect ServiceNow"
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
