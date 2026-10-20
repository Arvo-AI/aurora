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

// Shape only: an https origin with no path. Which hosts are accepted
// (*.service-now.com, *.servicenowservices.com and SERVICENOW_ALLOWED_HOSTS) is
// decided by the server's normalize_instance_url, so the two never disagree.
const INSTANCE_URL_PATTERN = /^https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)+\/?$/i;

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
                Use the form https://&lt;instance&gt;.service-now.com (or your instance&apos;s own host), with no path
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
                Service account (username and password)
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
              <details className="rounded-md border px-3 py-2 text-sm">
                <summary className="cursor-pointer font-medium">How to create the service account</summary>
                <ol className="mt-2 list-decimal space-y-1 pl-5 text-muted-foreground">
                  <li>
                    In ServiceNow open <span className="font-medium text-foreground">User Administration &gt; Users &gt; New</span>.
                    Create a dedicated user (for example <code>aurora</code>), tick{" "}
                    <span className="font-medium text-foreground">Web service access only</span>, then use{" "}
                    <span className="font-medium text-foreground">Set Password</span> and save the user.
                  </li>
                  <li>
                    Under <span className="font-medium text-foreground">Roles</span> add <code>itil</code> (read and write
                    incidents). On Yokohama, Zurich and later also add <code>snc_basic_auth_api_access</code>, or the
                    instance refuses basic auth on the REST API with &quot;Authentication failed&quot;.
                  </li>
                  <li>Enter that user name and password below.</li>
                </ol>
              </details>
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
              <details className="rounded-md border px-3 py-2 text-sm">
                <summary className="cursor-pointer font-medium">How to register the OAuth client</summary>
                <ol className="mt-2 list-decimal space-y-1 pl-5 text-muted-foreground">
                  <li>
                    Create the integration user first (see the service-account steps) with the <code>itil</code> role.
                  </li>
                  <li>
                    Open <span className="font-medium text-foreground">System OAuth &gt; Application Registry &gt; New &gt; Create
                    an OAuth API endpoint for external clients</span>. Name it (for example Aurora) and copy the{" "}
                    <span className="font-medium text-foreground">Client ID</span> and{" "}
                    <span className="font-medium text-foreground">Client Secret</span>.
                  </li>
                  <li>
                    Set <span className="font-medium text-foreground">OAuth Application User</span> to the integration user.
                    The client-credentials grant must be enabled on the instance: system property{" "}
                    <code>glide.oauth.inbound.client.credential.grant_type.enabled</code> = <code>true</code>.
                  </li>
                  <li>Enter the Client ID and Client Secret below. Aurora requests tokens from <code>/oauth_token.do</code>.</li>
                </ol>
              </details>
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
            Aurora validates the connection by reading one incident before storing anything. Credentials are stored in
            Vault; OAuth tokens are kept in memory only.
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
