"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import { useGitLabStatus } from "@/hooks/use-gitlab-status";
import { ArrowLeft, SpinnerGap, Check, SignOut, PencilSimple, X, ArrowClockwise } from "@phosphor-icons/react";

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
  const { baseUrl, refresh } = useGitLabStatus();

  const [tokenInput, setTokenInput] = useState("");
  const [baseUrlInput, setBaseUrlInput] = useState("https://gitlab.com");
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [connectedUsername, setConnectedUsername] = useState<string | null>(null);
  const [connectedProjects, setConnectedProjects] = useState<ConnectedProject[]>([]);
  const [editingMetadata, setEditingMetadata] = useState<Record<string, string>>({});
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchProjects = async () => {
    const res = await fetch("/api/proxy/gitlab/repo-selections");
    if (!res.ok) return [];
    const data = await res.json();
    return (data.repositories || []) as ConnectedProject[];
  };

  const startMetadataPolling = useCallback((projects: ConnectedProject[]) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    const hasPending = projects.some(p => p.metadata_status === "pending" || p.metadata_status === "generating");
    if (!hasPending) return;
    pollingRef.current = setInterval(async () => {
      try {
        const updated = await fetchProjects();
        setConnectedProjects(updated);
        const stillPending = updated.some(p => p.metadata_status === "pending" || p.metadata_status === "generating");
        if (!stillPending && pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      } catch {}
    }, 3000);
  }, []);

  useEffect(() => () => { if (pollingRef.current) clearInterval(pollingRef.current); }, []);

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/proxy/gitlab/status");
      const creds = res.ok ? await res.json() : { connected: false };
      setIsAuthenticated(creds.connected);
      setConnectedUsername(creds.username || null);
      if (creds.base_url) setBaseUrlInput(creds.base_url);

      if (creds.connected) {
        const projects = await fetchProjects();
        setConnectedProjects(projects);
        startMetadataPolling(projects);
      }
    } catch {
      setIsAuthenticated(false);
    }
  }, [startMetadataPolling]);

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
        window.dispatchEvent(new Event("gitlabStateChanged"));
        refresh();
        const projects = await fetchProjects();
        setConnectedProjects(projects);
        startMetadataPolling(projects);
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
      window.dispatchEvent(new Event("gitlabStateChanged"));
      toast({ title: "Disconnected", description: "GitLab has been disconnected" });
      refresh();
    } catch {
      toast({ title: "Error", description: "Failed to disconnect", variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleMetadataSave = async (repoFullName: string) => {
    const summary = editingMetadata[repoFullName];
    if (summary === undefined) return;
    try {
      const res = await fetch(`/api/proxy/gitlab/repo-selections/${encodeURIComponent(repoFullName)}/metadata`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ metadata_summary: summary }),
      });
      if (!res.ok) throw new Error();
      setConnectedProjects(prev => prev.map(p =>
        p.repo_full_name === repoFullName ? { ...p, metadata_summary: summary, metadata_status: "ready" } : p
      ));
      setEditingMetadata(prev => { const n = { ...prev }; delete n[repoFullName]; return n; });
      toast({ title: "Description updated" });
    } catch {
      toast({ title: "Error", description: "Failed to update description", variant: "destructive" });
    }
  };

  const handleRegenerate = async (repoFullName: string) => {
    try {
      const res = await fetch("/api/proxy/gitlab/repo-metadata/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_full_name: repoFullName }),
      });
      if (!res.ok) throw new Error();
      const updated = connectedProjects.map(p =>
        p.repo_full_name === repoFullName ? { ...p, metadata_status: "generating" } : p
      );
      setConnectedProjects(updated);
      startMetadataPolling(updated);
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
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
                  {isConnecting ? <><SpinnerGap className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect GitLab"}
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
                  {isDisconnecting ? <SpinnerGap className="h-4 w-4 animate-spin" /> : <SignOut className="h-4 w-4" />}
                </Button>
              </div>

              {connectedProjects.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-medium">Connected Projects ({connectedProjects.length})</h4>
                  <p className="text-xs text-muted-foreground">
                    All projects accessible by the token are automatically connected. To change scope, update the token permissions in GitLab.
                  </p>
                  <div className="space-y-2 max-h-[500px] overflow-y-auto">
                    {connectedProjects.map(p => (
                      <div key={p.repo_full_name} className="p-2 rounded-md border border-border space-y-1">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium">{p.repo_full_name}</span>
                            {p.is_private && <Badge variant="secondary" className="text-xs">Private</Badge>}
                          </div>
                          <div className="flex items-center gap-1">
                            {p.metadata_status === "ready" && (
                              <>
                                <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => {
                                  if (editingMetadata[p.repo_full_name] !== undefined) {
                                    setEditingMetadata(prev => { const n = { ...prev }; delete n[p.repo_full_name]; return n; });
                                  } else {
                                    setEditingMetadata(prev => ({ ...prev, [p.repo_full_name]: p.metadata_summary || "" }));
                                  }
                                }} title="Edit description">
                                  {editingMetadata[p.repo_full_name] !== undefined ? <X className="h-3 w-3" /> : <PencilSimple className="h-3 w-3" />}
                                </Button>
                                <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => handleRegenerate(p.repo_full_name)} title="Regenerate">
                                  <ArrowClockwise className="h-3 w-3" />
                                </Button>
                              </>
                            )}
                            {p.metadata_status === "error" && (
                              <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => handleRegenerate(p.repo_full_name)}>Retry</Button>
                            )}
                          </div>
                        </div>

                        {(p.metadata_status === "pending" || p.metadata_status === "generating") && (
                          <div className="flex items-center gap-2 text-xs text-muted-foreground">
                            <SpinnerGap className="h-3 w-3 animate-spin" />Generating description...
                          </div>
                        )}
                        {p.metadata_status === "error" && (
                          <p className="text-xs text-red-500">Failed to generate description</p>
                        )}
                        {p.metadata_status === "ready" && editingMetadata[p.repo_full_name] !== undefined && (
                          <div className="space-y-1">
                            <Textarea
                              value={editingMetadata[p.repo_full_name]}
                              onChange={(e) => setEditingMetadata(prev => ({ ...prev, [p.repo_full_name]: e.target.value }))}
                              className="text-xs min-h-[60px]"
                              rows={3}
                            />
                            <div className="flex gap-1 justify-end">
                              <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setEditingMetadata(prev => { const n = { ...prev }; delete n[p.repo_full_name]; return n; })}>Cancel</Button>
                              <Button size="sm" className="h-6 px-2 text-xs" onClick={() => handleMetadataSave(p.repo_full_name)}>Save</Button>
                            </div>
                          </div>
                        )}
                        {p.metadata_status === "ready" && editingMetadata[p.repo_full_name] === undefined && p.metadata_summary && (
                          <p className="text-xs text-muted-foreground">{p.metadata_summary.replace(/\*\*/g, "")}</p>
                        )}
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
