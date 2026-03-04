"use client";

import { useEffect, useState } from "react";
import { Loader2, CheckCircle, Shield, ShieldAlert, ExternalLink } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { atlassianService, AtlassianStatus } from "@/lib/services/atlassian";
import { jiraService } from "@/lib/services/jira";
import { isConfluenceEnabled } from "@/lib/feature-flags";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";

type AgentTier = "read" | "write";

export default function JiraConnectPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<AtlassianStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [agentTier, setAgentTier] = useState<AgentTier>("read");
  const [alsoConnectConfluence, setAlsoConnectConfluence] = useState(false);

  const [patUrl, setPatUrl] = useState("");
  const [patToken, setPatToken] = useState("");
  const [isPatConnecting, setIsPatConnecting] = useState(false);

  const jiraConnected = status?.jira?.connected ?? false;
  const confluenceConnected = status?.confluence?.connected ?? false;

  const loadStatus = async () => {
    setIsLoading(true);
    try {
      const result = await atlassianService.getStatus();
      setStatus(result);
      if (result?.jira?.agentTier) setAgentTier(result.jira.agentTier);
      if (result?.jira?.connected) {
        localStorage.setItem("isJiraConnected", "true");
      } else {
        localStorage.removeItem("isJiraConnected");
      }
    } catch {
      // silent
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => { loadStatus(); }, []);

  const handleOAuthConnect = async () => {
    setIsConnecting(true);
    try {
      const products = ["jira"];
      if (alsoConnectConfluence && !confluenceConnected) products.push("confluence");
      const result = await atlassianService.connect({ products, authType: "oauth", agentTier });
      if (result?.authUrl) {
        window.location.href = result.authUrl;
        return;
      }
      if (result?.connected || result?.success) {
        await loadStatus();
        toast({ title: "Jira connected" });
        localStorage.setItem("isJiraConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err) {
      toast({ title: "Failed to connect", description: err instanceof Error ? err.message : "OAuth failed", variant: "destructive" });
    } finally {
      setIsConnecting(false);
    }
  };

  const handlePatConnect = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!patUrl || !patToken) return;
    setIsPatConnecting(true);
    try {
      await atlassianService.connect({
        products: ["jira"],
        authType: "pat",
        jiraBaseUrl: patUrl,
        jiraPatToken: patToken,
        agentTier,
      });
      await loadStatus();
      toast({ title: "Jira connected via PAT" });
      localStorage.setItem("isJiraConnected", "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
      setPatToken("");
    } catch (err) {
      toast({ title: "Failed to connect", description: err instanceof Error ? err.message : "PAT failed", variant: "destructive" });
    } finally {
      setIsPatConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await atlassianService.disconnect("jira");
      await loadStatus();
      localStorage.removeItem("isJiraConnected");
      toast({ title: "Jira disconnected" });
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      toast({ title: "Failed to disconnect", description: err instanceof Error ? err.message : "Failed", variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleTierChange = async (tier: AgentTier) => {
    setAgentTier(tier);
    if (jiraConnected) {
      try {
        await jiraService.updateSettings({ agentTier: tier });
        toast({ title: "Agent tier updated", description: tier === "write" ? "Full access enabled" : "Read-only mode" });
      } catch {
        toast({ title: "Failed to update tier", variant: "destructive" });
      }
    }
  };

  if (isLoading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Jira</h1>
          <p className="text-muted-foreground mt-1">Connect Jira for issue tracking and incident management</p>
        </div>
        <Card>
          <CardContent className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-2xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Jira</h1>
        <p className="text-muted-foreground mt-1">Search issues, track incidents, and export postmortem action items</p>
      </div>

      {jiraConnected ? (
        <>
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2">
                  <CheckCircle className="h-5 w-5 text-green-600" />
                  Connected
                </CardTitle>
                <span className="text-xs font-medium text-green-600 bg-green-50 dark:bg-green-950 px-2 py-1 rounded">
                  {status?.jira?.authType === "pat" ? "PAT" : "OAuth"}
                </span>
              </div>
              {status?.jira?.baseUrl && (
                <CardDescription>{status.jira.baseUrl}</CardDescription>
              )}
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <Label className="text-sm font-medium">Agent Permission Tier</Label>
                <p className="text-xs text-muted-foreground mb-3">
                  Controls what the Aurora agent can do with your Jira
                </p>
                <div className="space-y-2">
                  <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                    <input type="radio" name="agentTier" value="read" checked={agentTier === "read"} onChange={() => handleTierChange("read")} className="mt-0.5" />
                    <div>
                      <div className="flex items-center gap-1.5 font-medium text-sm">
                        <Shield className="h-4 w-4" /> Read Only
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        Search issues, view details, add comments. Cannot create or modify issues.
                      </p>
                    </div>
                  </label>
                  <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                    <input type="radio" name="agentTier" value="write" checked={agentTier === "write"} onChange={() => handleTierChange("write")} className="mt-0.5" />
                    <div>
                      <div className="flex items-center gap-1.5 font-medium text-sm">
                        <ShieldAlert className="h-4 w-4" /> Full Access
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        Everything in Read, plus create issues, update fields, and link issues.
                      </p>
                    </div>
                  </label>
                </div>
              </div>
            </CardContent>
            <CardFooter>
              <Button variant="destructive" size="sm" onClick={handleDisconnect} disabled={isDisconnecting}>
                {isDisconnecting ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Disconnecting...</> : "Disconnect Jira"}
              </Button>
            </CardFooter>
          </Card>

          {isConfluenceEnabled() && !confluenceConnected && (
            <Card className="border-dashed">
              <CardHeader>
                <CardTitle className="text-base">Also connect Confluence?</CardTitle>
                <CardDescription>Fetch runbooks, documentation, and export postmortems. Uses the same Atlassian account.</CardDescription>
              </CardHeader>
              <CardFooter>
                <a href="/confluence/connect">
                  <Button variant="outline" size="sm">
                    <ExternalLink className="mr-2 h-4 w-4" /> Set up Confluence
                  </Button>
                </a>
              </CardFooter>
            </Card>
          )}
        </>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Jira Cloud (OAuth)</CardTitle>
              <CardDescription>Connect your Atlassian Cloud Jira instance using OAuth 2.0.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <Label className="text-sm font-medium">Agent Permission Tier</Label>
                <div className="flex gap-4 mt-2">
                  <label className="flex items-center gap-1.5 text-sm cursor-pointer">
                    <input type="radio" name="tierInit" value="read" checked={agentTier === "read"} onChange={() => setAgentTier("read")} />
                    <Shield className="h-3.5 w-3.5" /> Read Only
                  </label>
                  <label className="flex items-center gap-1.5 text-sm cursor-pointer">
                    <input type="radio" name="tierInit" value="write" checked={agentTier === "write"} onChange={() => setAgentTier("write")} />
                    <ShieldAlert className="h-3.5 w-3.5" /> Full Access
                  </label>
                </div>
              </div>

              {isConfluenceEnabled() && !confluenceConnected && (
                <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                  <Checkbox checked={alsoConnectConfluence} onCheckedChange={() => setAlsoConnectConfluence(!alsoConnectConfluence)} />
                  <div>
                    <div className="font-medium text-sm">Also connect Confluence</div>
                    <p className="text-xs text-muted-foreground">Fetch runbooks and export postmortems. Same OAuth, no extra login.</p>
                  </div>
                </label>
              )}
            </CardContent>
            <CardFooter>
              <Button onClick={handleOAuthConnect} disabled={isConnecting}>
                {isConnecting ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Redirecting...</> : "Connect with Atlassian"}
              </Button>
            </CardFooter>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Jira Data Center (PAT)</CardTitle>
              <CardDescription>Connect a self-hosted Jira instance using a personal access token.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePatConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="jiraPatUrl">Base URL</Label>
                  <Input id="jiraPatUrl" type="url" placeholder="https://jira.yourcompany.com" value={patUrl} onChange={(e) => setPatUrl(e.target.value)} required />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="jiraPatToken">Personal Access Token</Label>
                  <Input id="jiraPatToken" type="password" placeholder="Your Jira PAT" value={patToken} onChange={(e) => setPatToken(e.target.value)} required />
                </div>
                <Button type="submit" variant="outline" disabled={isPatConnecting} className="w-full">
                  {isPatConnecting ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Connecting...</> : "Connect with PAT"}
                </Button>
              </form>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
