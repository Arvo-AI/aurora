"use client";

import { useEffect, useState } from "react";
import { Loader2, CheckCircle, Shield, ShieldAlert } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { atlassianService, AtlassianStatus, AtlassianProductStatus } from "@/lib/services/atlassian";
import { jiraService } from "@/lib/services/jira";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";

type AgentTier = "read" | "write";

export default function AtlassianConnectPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<AtlassianStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedProducts, setSelectedProducts] = useState<string[]>(["confluence", "jira"]);
  const [isOauthConnecting, setIsOauthConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState<Record<string, boolean>>({});
  const [agentTier, setAgentTier] = useState<AgentTier>("read");

  const [confPatUrl, setConfPatUrl] = useState("");
  const [confPatToken, setConfPatToken] = useState("");
  const [jiraPatUrl, setJiraPatUrl] = useState("");
  const [jiraPatToken, setJiraPatToken] = useState("");
  const [isPatConnecting, setIsPatConnecting] = useState(false);

  const confluenceConnected = status?.confluence?.connected ?? false;
  const jiraConnected = status?.jira?.connected ?? false;
  const anyConnected = confluenceConnected || jiraConnected;

  const loadStatus = async () => {
    setIsLoading(true);
    try {
      const result = await atlassianService.getStatus();
      setStatus(result);
      if (result?.jira?.agentTier) {
        setAgentTier(result.jira.agentTier);
      }
      const connected = result?.confluence?.connected || result?.jira?.connected;
      if (connected) {
        localStorage.setItem("isAtlassianConnected", "true");
      } else {
        localStorage.removeItem("isAtlassianConnected");
      }
    } catch {
      const cached = localStorage.getItem("isAtlassianConnected") === "true";
      if (cached) {
        setStatus({
          confluence: { connected: true },
          jira: { connected: true },
        } as AtlassianStatus);
      }
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const toggleProduct = (product: string) => {
    setSelectedProducts((prev) =>
      prev.includes(product) ? prev.filter((p) => p !== product) : [...prev, product]
    );
  };

  const handleOAuthConnect = async () => {
    if (selectedProducts.length === 0) {
      toast({ title: "Select at least one product", variant: "destructive" });
      return;
    }
    setIsOauthConnecting(true);
    try {
      const result = await atlassianService.connect({
        products: selectedProducts,
        authType: "oauth",
        agentTier,
      });
      if (result?.authUrl) {
        window.location.href = result.authUrl;
        return;
      }
      if (result?.connected || result?.success) {
        await loadStatus();
        toast({ title: "Atlassian connected", description: "Connection established." });
        localStorage.setItem("isAtlassianConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "OAuth connection failed";
      toast({ title: "Failed to connect", description: message, variant: "destructive" });
    } finally {
      setIsOauthConnecting(false);
    }
  };

  const handlePatConnect = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsPatConnecting(true);
    try {
      const payload: Record<string, unknown> = {
        products: selectedProducts,
        authType: "pat",
        agentTier,
      };
      if (selectedProducts.includes("confluence")) {
        payload.confluenceBaseUrl = confPatUrl;
        payload.confluencePatToken = confPatToken;
      }
      if (selectedProducts.includes("jira")) {
        payload.jiraBaseUrl = jiraPatUrl;
        payload.jiraPatToken = jiraPatToken;
      }
      await atlassianService.connect(payload as never);
      await loadStatus();
      toast({ title: "Connected via PAT" });
      localStorage.setItem("isAtlassianConnected", "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
      setConfPatToken("");
      setJiraPatToken("");
    } catch (err) {
      const message = err instanceof Error ? err.message : "PAT connection failed";
      toast({ title: "Failed to connect", description: message, variant: "destructive" });
    } finally {
      setIsPatConnecting(false);
    }
  };

  const handleDisconnect = async (product: "confluence" | "jira") => {
    setIsDisconnecting((prev) => ({ ...prev, [product]: true }));
    try {
      await atlassianService.disconnect(product);
      await loadStatus();
      toast({ title: `${product === "confluence" ? "Confluence" : "Jira"} disconnected` });
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Disconnect failed";
      toast({ title: "Failed to disconnect", description: message, variant: "destructive" });
    } finally {
      setIsDisconnecting((prev) => ({ ...prev, [product]: false }));
    }
  };

  const handleTierChange = async (tier: AgentTier) => {
    setAgentTier(tier);
    if (jiraConnected) {
      try {
        await jiraService.updateSettings({ agentTier: tier });
        toast({ title: "Jira agent tier updated", description: tier === "write" ? "Full access enabled" : "Read-only mode" });
      } catch {
        toast({ title: "Failed to update tier", variant: "destructive" });
      }
    }
  };

  const renderProductStatus = (product: string, info: AtlassianProductStatus | undefined) => {
    const label = product === "confluence" ? "Confluence" : "Jira";
    const connected = info?.connected ?? false;

    return (
      <Card key={product}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              {connected && <CheckCircle className="h-5 w-5 text-green-600" />}
              {label}
            </CardTitle>
            {connected && (
              <span className="text-xs font-medium text-green-600 bg-green-50 dark:bg-green-950 px-2 py-1 rounded">
                Connected
              </span>
            )}
          </div>
          {connected && (
            <CardDescription>
              {info?.authType === "pat" ? "Personal Access Token" : "OAuth 2.0"}{info?.baseUrl ? ` — ${info.baseUrl}` : ""}
            </CardDescription>
          )}
        </CardHeader>

        {product === "jira" && connected && (
          <CardContent className="space-y-4">
            <div>
              <Label className="text-sm font-medium">Agent Permission Tier</Label>
              <p className="text-xs text-muted-foreground mb-3">
                Controls what the Aurora agent can do with Jira
              </p>
              <div className="space-y-2">
                <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                  <input
                    type="radio"
                    name="agentTier"
                    value="read"
                    checked={agentTier === "read"}
                    onChange={() => handleTierChange("read")}
                    className="mt-0.5"
                  />
                  <div>
                    <div className="flex items-center gap-1.5 font-medium text-sm">
                      <Shield className="h-4 w-4" />
                      Read Only
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Search issues, view details, add comments. Cannot create or modify issues.
                    </p>
                  </div>
                </label>
                <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                  <input
                    type="radio"
                    name="agentTier"
                    value="write"
                    checked={agentTier === "write"}
                    onChange={() => handleTierChange("write")}
                    className="mt-0.5"
                  />
                  <div>
                    <div className="flex items-center gap-1.5 font-medium text-sm">
                      <ShieldAlert className="h-4 w-4" />
                      Full Access
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Everything in Read, plus create issues, update fields, and link issues.
                    </p>
                  </div>
                </label>
              </div>
            </div>
          </CardContent>
        )}

        {connected && (
          <CardFooter>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => handleDisconnect(product as "confluence" | "jira")}
              disabled={isDisconnecting[product]}
            >
              {isDisconnecting[product] ? (
                <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Disconnecting...</>
              ) : (
                `Disconnect ${label}`
              )}
            </Button>
          </CardFooter>
        )}
      </Card>
    );
  };

  if (isLoading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Atlassian Integration</h1>
          <p className="text-muted-foreground mt-1">Connect Confluence and Jira with one Atlassian account</p>
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
    <div className="container mx-auto py-8 px-4 max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Atlassian Integration</h1>
        <p className="text-muted-foreground mt-1">Connect Confluence and Jira with one Atlassian account</p>
      </div>

      {anyConnected && (
        <div className="space-y-4">
          {renderProductStatus("confluence", status?.confluence)}
          {renderProductStatus("jira", status?.jira)}

          {(!confluenceConnected || !jiraConnected) && (
            <Card>
              <CardHeader>
                <CardTitle>Add {!confluenceConnected ? "Confluence" : "Jira"}</CardTitle>
                <CardDescription>
                  Expand your Atlassian connection to include {!confluenceConnected ? "Confluence for runbooks and documentation" : "Jira for issue tracking and incident management"}.
                </CardDescription>
              </CardHeader>
              <CardFooter>
                <Button
                  onClick={() => {
                    const missing = !confluenceConnected ? "confluence" : "jira";
                    setSelectedProducts([missing]);
                    handleOAuthConnect();
                  }}
                  disabled={isOauthConnecting}
                >
                  {isOauthConnecting ? (
                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Redirecting...</>
                  ) : (
                    `Add ${!confluenceConnected ? "Confluence" : "Jira"}`
                  )}
                </Button>
              </CardFooter>
            </Card>
          )}
        </div>
      )}

      {!anyConnected && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Select Products</CardTitle>
              <CardDescription>Choose which Atlassian products to connect. You can add more later.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                <Checkbox
                  checked={selectedProducts.includes("confluence")}
                  onCheckedChange={() => toggleProduct("confluence")}
                />
                <div>
                  <div className="font-medium text-sm">Confluence</div>
                  <p className="text-xs text-muted-foreground">Fetch runbooks, documentation, and export postmortems</p>
                </div>
              </label>
              <label className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                <Checkbox
                  checked={selectedProducts.includes("jira")}
                  onCheckedChange={() => toggleProduct("jira")}
                />
                <div>
                  <div className="font-medium text-sm">Jira</div>
                  <p className="text-xs text-muted-foreground">Search issues, track incidents, export action items</p>
                </div>
              </label>

              {selectedProducts.includes("jira") && (
                <div className="ml-8 mt-2">
                  <Label className="text-xs font-medium text-muted-foreground">Jira Agent Permission Tier</Label>
                  <div className="flex gap-4 mt-1">
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
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Atlassian Cloud (OAuth)</CardTitle>
              <CardDescription>Connect your Atlassian Cloud site using OAuth 2.0. One authorization for all selected products.</CardDescription>
            </CardHeader>
            <CardFooter>
              <Button onClick={handleOAuthConnect} disabled={isOauthConnecting || selectedProducts.length === 0}>
                {isOauthConnecting ? (
                  <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Redirecting...</>
                ) : (
                  "Connect with Atlassian"
                )}
              </Button>
            </CardFooter>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Data Center (PAT)</CardTitle>
              <CardDescription>Connect self-hosted instances using personal access tokens.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePatConnect} className="space-y-6">
                {selectedProducts.includes("confluence") && (
                  <div className="space-y-3">
                    <h4 className="text-sm font-medium">Confluence</h4>
                    <div className="space-y-2">
                      <Label htmlFor="confPatUrl">Base URL</Label>
                      <Input id="confPatUrl" type="url" placeholder="https://confluence.internal" value={confPatUrl} onChange={(e) => setConfPatUrl(e.target.value)} required />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="confPatToken">Personal Access Token</Label>
                      <Input id="confPatToken" type="password" placeholder="Confluence PAT" value={confPatToken} onChange={(e) => setConfPatToken(e.target.value)} required />
                    </div>
                  </div>
                )}
                {selectedProducts.includes("jira") && (
                  <div className="space-y-3">
                    <h4 className="text-sm font-medium">Jira</h4>
                    <div className="space-y-2">
                      <Label htmlFor="jiraPatUrl">Base URL</Label>
                      <Input id="jiraPatUrl" type="url" placeholder="https://jira.internal" value={jiraPatUrl} onChange={(e) => setJiraPatUrl(e.target.value)} required />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="jiraPatToken">Personal Access Token</Label>
                      <Input id="jiraPatToken" type="password" placeholder="Jira PAT" value={jiraPatToken} onChange={(e) => setJiraPatToken(e.target.value)} required />
                    </div>
                  </div>
                )}
                <Button type="submit" disabled={isPatConnecting || selectedProducts.length === 0} className="w-full">
                  {isPatConnecting ? (
                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Connecting...</>
                  ) : (
                    "Connect with PAT"
                  )}
                </Button>
              </form>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
