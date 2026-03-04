"use client";

import { useEffect, useState } from "react";
import { Loader2, CheckCircle, ExternalLink } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { atlassianService, AtlassianStatus } from "@/lib/services/atlassian";
import { isJiraEnabled } from "@/lib/feature-flags";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";

export default function ConfluenceConnectPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<AtlassianStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [alsoConnectJira, setAlsoConnectJira] = useState(false);

  const [patUrl, setPatUrl] = useState("");
  const [patToken, setPatToken] = useState("");
  const [isPatConnecting, setIsPatConnecting] = useState(false);

  const confluenceConnected = status?.confluence?.connected ?? false;
  const jiraConnected = status?.jira?.connected ?? false;

  const loadStatus = async () => {
    setIsLoading(true);
    try {
      const result = await atlassianService.getStatus();
      setStatus(result);
      if (result?.confluence?.connected) {
        localStorage.setItem("isConfluenceConnected", "true");
      } else {
        localStorage.removeItem("isConfluenceConnected");
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
      const products = ["confluence"];
      if (alsoConnectJira && !jiraConnected) products.push("jira");
      const result = await atlassianService.connect({ products, authType: "oauth" });
      if (result?.authUrl) {
        window.location.href = result.authUrl;
        return;
      }
      if (result?.connected || result?.success) {
        await loadStatus();
        toast({ title: "Confluence connected" });
        localStorage.setItem("isConfluenceConnected", "true");
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
        products: ["confluence"],
        authType: "pat",
        confluenceBaseUrl: patUrl,
        confluencePatToken: patToken,
      });
      await loadStatus();
      toast({ title: "Confluence connected via PAT" });
      localStorage.setItem("isConfluenceConnected", "true");
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
      await atlassianService.disconnect("confluence");
      await loadStatus();
      localStorage.removeItem("isConfluenceConnected");
      toast({ title: "Confluence disconnected" });
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      toast({ title: "Failed to disconnect", description: err instanceof Error ? err.message : "Failed", variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Confluence</h1>
          <p className="text-muted-foreground mt-1">Connect Confluence for runbooks and documentation</p>
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
        <h1 className="text-3xl font-bold">Confluence</h1>
        <p className="text-muted-foreground mt-1">Fetch runbooks, documentation, and export postmortems</p>
      </div>

      {confluenceConnected ? (
        <>
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2">
                  <CheckCircle className="h-5 w-5 text-green-600" />
                  Connected
                </CardTitle>
                <span className="text-xs font-medium text-green-600 bg-green-50 dark:bg-green-950 px-2 py-1 rounded">
                  {status?.confluence?.authType === "pat" ? "PAT" : "OAuth"}
                </span>
              </div>
              {status?.confluence?.baseUrl && (
                <CardDescription>{status.confluence.baseUrl}</CardDescription>
              )}
            </CardHeader>
            <CardFooter>
              <Button variant="destructive" size="sm" onClick={handleDisconnect} disabled={isDisconnecting}>
                {isDisconnecting ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Disconnecting...</> : "Disconnect Confluence"}
              </Button>
            </CardFooter>
          </Card>

          {isJiraEnabled() && !jiraConnected && (
            <Card className="border-dashed">
              <CardHeader>
                <CardTitle className="text-base">Also connect Jira?</CardTitle>
                <CardDescription>Search issues, track incidents, and export action items. Uses the same Atlassian account.</CardDescription>
              </CardHeader>
              <CardFooter>
                <a href="/jira/connect">
                  <Button variant="outline" size="sm">
                    <ExternalLink className="mr-2 h-4 w-4" /> Set up Jira
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
              <CardTitle>Confluence Cloud (OAuth)</CardTitle>
              <CardDescription>Connect your Atlassian Cloud Confluence instance using OAuth 2.0.</CardDescription>
            </CardHeader>
            <CardContent>
              {isJiraEnabled() && !jiraConnected && (
                <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                  <Checkbox checked={alsoConnectJira} onCheckedChange={() => setAlsoConnectJira(!alsoConnectJira)} />
                  <div>
                    <div className="font-medium text-sm">Also connect Jira</div>
                    <p className="text-xs text-muted-foreground">Search issues and track incidents. Same OAuth, no extra login.</p>
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
              <CardTitle>Confluence Data Center (PAT)</CardTitle>
              <CardDescription>Connect a self-hosted Confluence instance using a personal access token.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePatConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="confPatUrl">Base URL</Label>
                  <Input id="confPatUrl" type="url" placeholder="https://confluence.yourcompany.com" value={patUrl} onChange={(e) => setPatUrl(e.target.value)} required />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="confPatToken">Personal Access Token</Label>
                  <Input id="confPatToken" type="password" placeholder="Your Confluence PAT" value={patToken} onChange={(e) => setPatToken(e.target.value)} required />
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
