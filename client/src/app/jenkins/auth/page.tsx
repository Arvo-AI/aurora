"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { jenkinsService, JenkinsStatus } from "@/lib/services/jenkins";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Check, Server, Unplug } from "lucide-react";

const getUserFriendlyError = (err: any): string => {
  if (!err) return "An unexpected error occurred. Please try again.";

  let errorText = "";
  if (typeof err.message === "string") {
    try {
      const parsed = JSON.parse(err.message);
      errorText = parsed.error || err.message;
    } catch {
      errorText = err.message;
    }
  } else if (err.error) {
    errorText = typeof err.error === "string" ? err.error : JSON.stringify(err.error);
  } else {
    errorText = err.message || err.toString() || "An unexpected error occurred";
  }

  errorText = errorText.replace(/^\d{3}\s+(Client|Server)\s+Error:\s*/i, "");
  if (errorText.length > 0) {
    errorText = errorText.charAt(0).toUpperCase() + errorText.slice(1);
  }
  return errorText || "An unexpected error occurred. Please try again.";
};

const CACHE_KEY = "jenkins_connection_status";

export default function JenkinsAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<JenkinsStatus | null>(null);
  const [loading, setLoading] = useState(false);

  const loadStatus = async () => {
    try {
      if (typeof window !== "undefined") {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
          if (parsed?.connected) {
            setBaseUrl(parsed.baseUrl ?? "");
            setUsername(parsed.username ?? "");
          }
        }
      }
      const result = await jenkinsService.getStatus();
      setStatus(result);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
      }
      if (result?.connected) {
        setBaseUrl(result.baseUrl ?? "");
        setUsername(result.username ?? "");
      }
    } catch (err) {
      console.error("Failed to load Jenkins status", err);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);

    try {
      const result = await jenkinsService.connect({ baseUrl, username, apiToken });
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        localStorage.setItem("isJenkinsConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "jenkins" }),
        });
      } catch (prefErr) {
        console.warn("Failed to update provider preferences", prefErr);
      }

      toast({
        title: "Success",
        description: "Jenkins connected successfully!",
      });
    } catch (err: any) {
      console.error("Jenkins connection failed", err);
      toast({
        title: "Failed to connect to Jenkins",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setApiToken("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/jenkins", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setBaseUrl("");
        setUsername("");

        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isJenkinsConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        try {
          await fetch("/api/provider-preferences", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", provider: "jenkins" }),
          });
        } catch (prefErr) {
          console.warn("Failed to update provider preferences", prefErr);
        }

        toast({ title: "Success", description: "Jenkins disconnected successfully" });
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Jenkins");
      }
    } catch (err: any) {
      console.error("Jenkins disconnect failed", err);
      toast({
        title: "Failed to disconnect Jenkins",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Jenkins Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your Jenkins instance to view jobs, builds, and pipeline status
        </p>
      </div>

      {status?.connected ? (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Check className="h-5 w-5 text-green-600" />
                  Jenkins Connected
                </CardTitle>
                <CardDescription className="mt-1">
                  Your Jenkins instance is connected and ready to use
                </CardDescription>
              </div>
              <Badge variant="outline" className="text-green-700 border-green-300 bg-green-50 dark:text-green-400 dark:border-green-700 dark:bg-green-950/30">
                Connected
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <p className="text-muted-foreground">URL</p>
                <p className="font-medium truncate">{status.baseUrl}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Username</p>
                <p className="font-medium">{status.username}</p>
              </div>
              {status.server?.version && (
                <div>
                  <p className="text-muted-foreground">Version</p>
                  <p className="font-medium">{status.server.version}</p>
                </div>
              )}
              {status.server?.mode && (
                <div>
                  <p className="text-muted-foreground">Mode</p>
                  <p className="font-medium capitalize">{status.server.mode}</p>
                </div>
              )}
              {status.server?.numExecutors !== undefined && (
                <div>
                  <p className="text-muted-foreground">Executors</p>
                  <p className="font-medium">{status.server.numExecutors}</p>
                </div>
              )}
            </div>

            <div className="pt-4 border-t">
              <Button
                variant="destructive"
                onClick={handleDisconnect}
                disabled={loading}
                className="w-full"
              >
                <Unplug className="h-4 w-4 mr-2" />
                {loading ? "Disconnecting..." : "Disconnect Jenkins"}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Server className="h-5 w-5" />
              Connect to Jenkins
            </CardTitle>
            <CardDescription>
              Enter your Jenkins URL, username, and API token to connect. You can
              generate an API token from your Jenkins user profile at{" "}
              <code className="text-xs bg-muted px-1 py-0.5 rounded">
                /me/configure
              </code>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="grid gap-2">
                <Label htmlFor="jenkins-url">Jenkins URL *</Label>
                <Input
                  id="jenkins-url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://jenkins.example.com"
                  required
                  disabled={loading}
                />
                <p className="text-xs text-muted-foreground">
                  The base URL of your Jenkins instance (no trailing slash)
                </p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="jenkins-username">Username *</Label>
                <Input
                  id="jenkins-username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="admin"
                  required
                  disabled={loading}
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="jenkins-token">API Token *</Label>
                <Input
                  id="jenkins-token"
                  type="password"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                  required
                  disabled={loading}
                />
                <p className="text-xs text-muted-foreground">
                  Generate at <code className="bg-muted px-1 py-0.5 rounded">Your Jenkins URL → People → Your User → Configure → API Token</code>. Stored securely in Vault.
                </p>
              </div>

              <div className="p-3 bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-800 rounded text-xs">
                <p className="font-medium text-amber-900 dark:text-amber-300 mb-1">Read-Only Access</p>
                <p className="text-amber-800 dark:text-amber-400">
                  This connector is read-only. Aurora will only read job and build information &mdash; it cannot trigger builds, modify jobs, or change Jenkins configuration.
                </p>
              </div>

              <div className="flex items-center justify-end pt-2">
                <Button type="submit" disabled={loading}>
                  {loading ? "Connecting..." : "Connect Jenkins"}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
