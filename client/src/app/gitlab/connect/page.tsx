"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";
import { useGitLabStatus } from "@/hooks/use-gitlab-status";
import { ArrowLeft, Loader2, Check, LogOut } from "lucide-react";

interface ConnectedProject {
  repo_full_name: string;
  repo_id: number;
  default_branch: string;
  is_private: boolean;
  metadata_summary: string | null;
  metadata_status: string;
  created_at: string | null;
}

export default function GitLabConnectPage() {
  const router = useRouter();
  const { toast } = useToast();
  const { baseUrl, refresh } = useGitLabStatus(null);

  const [tokenInput, setTokenInput] = useState("");
  const [baseUrlInput, setBaseUrlInput] = useState("https://gitlab.com");
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [connectedUsername, setConnectedUsername] = useState<string | null>(null);
  const [connectedProjects, setConnectedProjects] = useState<ConnectedProject[]>([]);

  const fetchProjects = async () => {
    const res = await fetch("/api/proxy/gitlab/repo-selections");
    if (!res.ok) return [];
    const data = await res.json();
    return (data.repositories || []) as ConnectedProject[];
  };

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/proxy/gitlab/status");
      const creds = res.ok ? await res.json() : { connected: false };
      setIsAuthenticated(creds.connected);
      setConnectedUsername(creds.username || null);
      if (creds.base_url) setBaseUrlInput(creds.base_url);

      if (creds.connected) {
        setConnectedProjects(await fetchProjects());
      }
    } catch {
      setIsAuthenticated(false);
    }
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  const handleConnect = async () => {
    if (!tokenInput.trim()) {
      toast({ title: "Error", description: "Please enter an access token", variant: "destructive" });
      return;
    }
    setIsConnecting(true);
    try {
      const res = await fetch("/api/proxy/gitlab/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token: tokenInput.trim(), base_url: baseUrlInput.trim() }),
      });
      const result = await res.json();

      if (result.success) {
        toast({ title: "Connected", description: `Connected to GitLab as ${result.username} — ${result.projects_connected} project(s) discovered` });
        setTokenInput("");
        setIsAuthenticated(true);
        setConnectedUsername(result.username || null);
        window.dispatchEvent(new Event("providerStateChanged"));
        refresh();
        setConnectedProjects(await fetchProjects());
      } else {
        toast({ title: "Error", description: result.error || "Failed to connect", variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Failed to connect to GitLab", variant: "destructive" });
    } finally {
      setIsConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      const res = await fetch("/api/proxy/gitlab/disconnect", { method: "POST" });
      if (!res.ok) throw new Error();
      setIsAuthenticated(false);
      setConnectedUsername(null);
      setConnectedProjects([]);
      window.dispatchEvent(new Event("providerStateChanged"));
      toast({ title: "Disconnected", description: "GitLab has been disconnected" });
      refresh();
    } catch {
      toast({ title: "Error", description: "Failed to disconnect", variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  return (
    <div className="container max-w-2xl mx-auto py-8 px-4">
      <Button
        variant="ghost"
        size="sm"
        className="mb-4"
        onClick={() => router.push("/connectors")}
      >
        <ArrowLeft className="h-4 w-4 mr-2" />
        Back to Connectors
      </Button>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            <img src="/gitlab.svg" alt="GitLab" className="h-7 w-7" />
            GitLab Integration
          </CardTitle>
          <CardDescription>
            Connect your GitLab instance using a Group Access Token.
            All projects accessible by the token will be automatically connected for RCA investigation.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!isAuthenticated ? (
            <div className="space-y-4">
              <div className="space-y-3">
                <div>
                  <label htmlFor="gitlab-base-url" className="text-sm font-medium">GitLab Instance URL</label>
                  <Input
                    value={baseUrlInput}
                    onChange={(e) => setBaseUrlInput(e.target.value)}
                    placeholder="https://gitlab.com"
                    className="mt-1"
                  />
                </div>

                <div>
                  <label htmlFor="gitlab-token" className="text-sm font-medium">Group Access Token</label>
                  <Input
                    id="gitlab-token"
                    type="password"
                    value={tokenInput}
                    onChange={(e) => setTokenInput(e.target.value)}
                    placeholder="glpat-..."
                    className="mt-1"
                  />
                  <div className="mt-2 space-y-1.5 text-xs text-muted-foreground">
                    <p>
                      Create a Group Access Token in GitLab under <strong>Group &gt; Settings &gt; Access Tokens</strong>.
                    </p>
                    <p className="font-medium text-foreground/80">Recommended (full capabilities):</p>
                    <p>Role: <code>Maintainer</code> · Scopes: <code>api</code> — allows RCA investigation, creating fix branches, pushing commits, and opening Merge Requests.</p>
                    <p className="font-medium text-foreground/80">Minimum (read-only investigation):</p>
                    <p>Role: <code>Reporter</code> · Scopes: <code>read_api</code> — allows viewing pipelines, commits, diffs, and merge requests. The agent will not be able to suggest or apply code fixes.</p>
                    <p>Only projects within the group (and its subgroups) will be connected.</p>
                  </div>
                </div>

                <Button onClick={handleConnect} disabled={isConnecting} className="w-full">
                  {isConnecting ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect GitLab"}
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center justify-between p-3 rounded-lg border bg-muted/30">
                <div className="flex items-center gap-2">
                  <Check className="h-4 w-4 text-green-500" />
                  <span className="text-sm font-medium">Connected as {connectedUsername}</span>
                  <Badge variant="secondary" className="text-xs">{baseUrl || baseUrlInput}</Badge>
                </div>
                <Button variant="ghost" size="sm" onClick={handleDisconnect} disabled={isDisconnecting}>
                  {isDisconnecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogOut className="h-4 w-4" />}
                </Button>
              </div>

              {connectedProjects.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium">Auto-connected Projects ({connectedProjects.length})</h4>
                  <p className="text-xs text-muted-foreground">
                    All projects accessible by the token are automatically connected. To change scope, update the token permissions in GitLab.
                  </p>
                  <div className="space-y-1 max-h-48 overflow-y-auto">
                    {connectedProjects.map(p => (
                      <div key={p.repo_full_name} className="flex items-center justify-between px-2 py-1.5 rounded text-sm bg-muted/20">
                        <span className="font-mono text-xs">{p.repo_full_name}</span>
                        <div className="flex items-center gap-1">
                          {p.is_private && <Badge variant="outline" className="text-xs">Private</Badge>}
                          <Badge variant="outline" className="text-xs">{p.default_branch}</Badge>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
