"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import { Loader2, CheckCircle, ExternalLink, PenLine, FilePlus2, AlertTriangle } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { atlassianService, AtlassianStatus } from "@/lib/services/atlassian";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";

interface ProductConfig {
  key: "jira" | "confluence";
  name: string;
  icon: string;
  subtitle: string;
  cloudLabel: string;
  dcLabel: string;
  patUrlPlaceholder: string;
  storageKey: string;
}

interface SiblingConfig {
  key: "jira" | "confluence";
  name: string;
  icon: string;
  subtitle: string;
  connectPath: string;
  enabled: boolean;
}

interface AtlassianConnectPageProps {
  product: ProductConfig;
  sibling?: SiblingConfig;
}

export function AtlassianConnectPage({ product, sibling }: AtlassianConnectPageProps) {
  const { toast } = useToast();
  const [status, setStatus] = useState<AtlassianStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [alsoConnectSibling, setAlsoConnectSibling] = useState(false);
  const [patUrl, setPatUrl] = useState("");
  const [patToken, setPatToken] = useState("");
  const [isPatConnecting, setIsPatConnecting] = useState(false);
  const [jiraMode, setJiraMode] = useState<"full" | "comment_only">("comment_only");
  const [isLoadingSettings, setIsLoadingSettings] = useState(false);
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  const [oauthConfigError, setOauthConfigError] = useState(false);

  const connected = status?.[product.key]?.connected ?? false;
  const siblingConnected = sibling ? (status?.[sibling.key]?.connected ?? false) : true;

  const loadStatus = async () => {
    setIsLoading(true);
    try {
      const result = await atlassianService.getStatus();
      setStatus(result);
      if (result?.[product.key]?.connected) localStorage.setItem(product.storageKey, "true");
      else localStorage.removeItem(product.storageKey);
    } catch { /* silent */ } finally { setIsLoading(false); }
  };

  useEffect(() => { loadStatus(); }, []);

  const loadJiraSettings = async () => {
    if (product.key !== "jira") return;
    setIsLoadingSettings(true);
    try {
      const res = await fetch("/api/jira/settings", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        if (data.jiraMode) setJiraMode(data.jiraMode);
      }
    } catch { /* silent */ } finally { setIsLoadingSettings(false); }
  };

  const saveJiraMode = async (mode: "full" | "comment_only") => {
    const previousMode = jiraMode;
    setJiraMode(mode);
    setIsSavingSettings(true);
    try {
      const res = await fetch("/api/jira/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ jiraMode: mode }),
      });
      if (res.ok) {
        toast({ title: "Settings saved", description: mode === "full" ? "Aurora can create issues and comment" : "Aurora will only comment on existing issues" });
      } else {
        setJiraMode(previousMode);
        toast({ title: "Failed to save settings", variant: "destructive" });
      }
    } catch {
      setJiraMode(previousMode);
      toast({ title: "Failed to save settings", variant: "destructive" });
    } finally { setIsSavingSettings(false); }
  };

  useEffect(() => { if (connected && product.key === "jira") loadJiraSettings(); }, [connected]);

  const handleOAuthConnect = async () => {
    setIsConnecting(true);
    try {
      const products: string[] = [product.key];
      if (sibling && alsoConnectSibling && !siblingConnected) products.push(sibling.key);
      const result = await atlassianService.connect({ products, authType: "oauth" });
      if (result?.authUrl) { window.location.href = result.authUrl; return; }
      if (result?.connected || result?.success) {
        await loadStatus();
        toast({ title: `${product.name} connected` });
        localStorage.setItem(product.storageKey, "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "OAuth failed";
      const isConfigMissing = msg.toLowerCase().includes("configuration missing") || msg.toLowerCase().includes("client_id");
      if (isConfigMissing) {
        setOauthConfigError(true);
      } else {
        toast({ title: "Connection failed", description: msg, variant: "destructive" });
      }
    } finally { setIsConnecting(false); }
  };

  const handlePatConnect = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!patUrl || !patToken) return;
    setIsPatConnecting(true);
    try {
      const payload: Record<string, unknown> = { products: [product.key], authType: "pat" as const };
      payload[`${product.key}BaseUrl`] = patUrl;
      payload[`${product.key}PatToken`] = patToken;
      await atlassianService.connect(payload as unknown as Parameters<typeof atlassianService.connect>[0]);
      await loadStatus();
      toast({ title: `${product.name} connected via PAT` });
      localStorage.setItem(product.storageKey, "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
      setPatToken("");
    } catch (err) {
      toast({ title: "Connection failed", description: err instanceof Error ? err.message : "PAT failed", variant: "destructive" });
    } finally { setIsPatConnecting(false); }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await atlassianService.disconnect(product.key);
      await loadStatus();
      localStorage.removeItem(product.storageKey);
      toast({ title: `${product.name} disconnected` });
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      toast({ title: "Disconnect failed", description: err instanceof Error ? err.message : "Failed", variant: "destructive" });
    } finally { setIsDisconnecting(false); }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="h-8 w-8 animate-spin text-[#2684FF]" />
      </div>
    );
  }

  return (
    <div className="container mx-auto py-10 px-4 max-w-xl space-y-8">
      <div className="flex items-center gap-4">
        <div className="h-14 w-14 rounded-xl bg-white border flex items-center justify-center p-2.5 shadow-sm">
          <Image src={product.icon} alt={product.name} width={36} height={36} />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{product.name}</h1>
          <p className="text-sm text-muted-foreground">{product.subtitle}</p>
        </div>
      </div>

      {connected ? (
        <div className="space-y-4">
          <Card className="border-[#2684FF]/30 bg-[#2684FF]/[0.03]">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <div className="h-8 w-8 rounded-full bg-[#2684FF]/10 flex items-center justify-center">
                    <CheckCircle className="h-4.5 w-4.5 text-[#2684FF]" />
                  </div>
                  <div>
                    <CardTitle className="text-base">Connected</CardTitle>
                    {status?.[product.key]?.baseUrl && (
                      <CardDescription className="text-xs">{status[product.key]!.baseUrl}</CardDescription>
                    )}
                  </div>
                </div>
                <span className="text-[10px] font-semibold uppercase tracking-wider text-[#2684FF] bg-[#2684FF]/10 px-2 py-1 rounded-full">
                  {status?.[product.key]?.authType === "pat" ? "PAT" : "OAuth"}
                </span>
              </div>
            </CardHeader>
            <CardFooter className="pt-0">
              <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive" onClick={handleDisconnect} disabled={isDisconnecting}>
                {isDisconnecting ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : null}
                Disconnect
              </Button>
            </CardFooter>
          </Card>

          {product.key === "jira" && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">RCA Permissions</CardTitle>
                <CardDescription className="text-xs">
                  Choose what Aurora can do with Jira during Root Cause Analysis
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                {isLoadingSettings ? (
                  <div className="flex items-center justify-center py-6">
                    <Loader2 className="h-5 w-5 animate-spin text-[#2684FF]" />
                  </div>
                ) : (
                  <>
                    <button
                      onClick={() => saveJiraMode("comment_only")}
                      disabled={isSavingSettings}
                      className={`w-full flex items-start gap-3 p-3 rounded-lg border text-left transition-colors ${
                        jiraMode === "comment_only"
                          ? "border-[#2684FF] bg-[#2684FF]/[0.04]"
                          : "border-border hover:bg-muted/50"
                      }`}
                    >
                      <div className={`mt-0.5 h-5 w-5 rounded-full border-2 flex items-center justify-center flex-shrink-0 ${
                        jiraMode === "comment_only" ? "border-[#2684FF]" : "border-muted-foreground/30"
                      }`}>
                        {jiraMode === "comment_only" && <div className="h-2.5 w-2.5 rounded-full bg-[#2684FF]" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <PenLine className="h-3.5 w-3.5 text-muted-foreground" />
                          <span className="text-sm font-medium">Comment only</span>
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          Only add comments to existing issues — no new issues or links
                        </p>
                      </div>
                    </button>

                    <button
                      onClick={() => saveJiraMode("full")}
                      disabled={isSavingSettings}
                      className={`w-full flex items-start gap-3 p-3 rounded-lg border text-left transition-colors ${
                        jiraMode === "full"
                          ? "border-[#2684FF] bg-[#2684FF]/[0.04]"
                          : "border-border hover:bg-muted/50"
                      }`}
                    >
                      <div className={`mt-0.5 h-5 w-5 rounded-full border-2 flex items-center justify-center flex-shrink-0 ${
                        jiraMode === "full" ? "border-[#2684FF]" : "border-muted-foreground/30"
                      }`}>
                        {jiraMode === "full" && <div className="h-2.5 w-2.5 rounded-full bg-[#2684FF]" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <FilePlus2 className="h-3.5 w-3.5 text-muted-foreground" />
                          <span className="text-sm font-medium">Create & comment</span>
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          Create new issues, link related issues, and comment on existing ones
                        </p>
                      </div>
                    </button>
                  </>
                )}
              </CardContent>
            </Card>
          )}

          {sibling?.enabled && !siblingConnected && (
            <a href={sibling.connectPath} className="block">
              <Card className="border-dashed hover:border-[#2684FF]/40 hover:bg-[#2684FF]/[0.02] transition-colors cursor-pointer">
                <CardHeader className="pb-2">
                  <div className="flex items-center gap-3">
                    <div className="h-9 w-9 rounded-lg bg-white border flex items-center justify-center p-1.5">
                      <Image src={sibling.icon} alt={sibling.name} width={22} height={22} />
                    </div>
                    <div>
                      <CardTitle className="text-sm">Add {sibling.name}</CardTitle>
                      <CardDescription className="text-xs">{sibling.subtitle} — same Atlassian account</CardDescription>
                    </div>
                    <ExternalLink className="h-4 w-4 text-muted-foreground ml-auto" />
                  </div>
                </CardHeader>
              </Card>
            </a>
          )}

          {sibling?.enabled && siblingConnected && sibling.key === "jira" && (
            <a href={sibling.connectPath} className="block">
              <Card className="border-[#2684FF]/20 hover:border-[#2684FF]/40 hover:bg-[#2684FF]/[0.02] transition-colors cursor-pointer">
                <CardHeader>
                  <div className="flex items-center gap-3">
                    <div className="h-9 w-9 rounded-lg bg-white border flex items-center justify-center p-1.5">
                      <Image src={sibling.icon} alt={sibling.name} width={22} height={22} />
                    </div>
                    <div>
                      <div className="flex items-center gap-1.5">
                        <CardTitle className="text-sm">Jira connected</CardTitle>
                        <CheckCircle className="h-3.5 w-3.5 text-[#2684FF]" />
                      </div>
                      <CardDescription className="text-xs">Configure RCA permissions for Jira</CardDescription>
                    </div>
                    <ExternalLink className="h-4 w-4 text-muted-foreground ml-auto" />
                  </div>
                </CardHeader>
              </Card>
            </a>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          {oauthConfigError && (
            <Card className="border-amber-500/40 bg-amber-500/[0.04]">
              <CardHeader className="pb-2">
                <div className="flex items-start gap-3">
                  <div className="h-8 w-8 rounded-full bg-amber-500/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400" />
                  </div>
                  <div className="space-y-1.5">
                    <CardTitle className="text-sm text-amber-700 dark:text-amber-300">Atlassian OAuth not configured</CardTitle>
                    <CardDescription className="text-xs leading-relaxed">
                      To connect via OAuth you need an Atlassian OAuth app. Add these to your <code className="px-1 py-0.5 rounded bg-muted text-[11px] font-mono">.env</code> file:
                    </CardDescription>
                    <div className="rounded-md bg-muted/80 px-3 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
                      ATLASSIAN_CLIENT_ID=your-client-id<br />
                      ATLASSIAN_CLIENT_SECRET=your-client-secret
                    </div>
                    <p className="text-xs text-muted-foreground">Then restart Aurora with <code className="px-1 py-0.5 rounded bg-muted text-[11px] font-mono">make down && make dev</code></p>
                  </div>
                </div>
              </CardHeader>
              <CardFooter className="pt-0 pl-14">
                <a
                  href="https://developer.atlassian.com/console/myapps/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs font-medium text-[#2684FF] hover:underline"
                >
                  Create Atlassian OAuth app
                  <ExternalLink className="h-3 w-3" />
                </a>
                <span className="mx-2 text-muted-foreground/40">|</span>
                <a
                  href="https://github.com/Arvo-AI/aurora/blob/main/website/docs/integrations/connectors.md#atlassian-confluence--jira"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:underline"
                >
                  Setup guide
                  <ExternalLink className="h-3 w-3" />
                </a>
              </CardFooter>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{product.cloudLabel}</CardTitle>
              <CardDescription>Connect via Atlassian OAuth</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 pt-0">
              {sibling?.enabled && !siblingConnected && (
                <label className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-muted/50 transition-colors">
                  <Checkbox checked={alsoConnectSibling} onCheckedChange={(checked) => setAlsoConnectSibling(checked === true)} />
                  <div className="flex items-center gap-2.5">
                    <Image src={sibling.icon} alt="" width={18} height={18} />
                    <span className="text-sm">Also connect {sibling.name}</span>
                  </div>
                </label>
              )}
            </CardContent>
            <CardFooter>
              <Button onClick={handleOAuthConnect} disabled={isConnecting} className="w-full bg-[#2684FF] hover:bg-[#0052CC] text-white">
                {isConnecting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                Connect with Atlassian
              </Button>
            </CardFooter>
          </Card>

          <div className="relative">
            <div className="absolute inset-0 flex items-center"><span className="w-full border-t" /></div>
            <div className="relative flex justify-center text-xs"><span className="bg-background px-2 text-muted-foreground">or</span></div>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{product.dcLabel}</CardTitle>
              <CardDescription>Connect via Personal Access Token</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePatConnect} className="space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor={`${product.key}PatUrl`} className="text-xs">Base URL</Label>
                  <Input id={`${product.key}PatUrl`} type="url" placeholder={product.patUrlPlaceholder} value={patUrl} onChange={(e) => setPatUrl(e.target.value)} required className="h-9" />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor={`${product.key}PatToken`} className="text-xs">Personal Access Token</Label>
                  <Input id={`${product.key}PatToken`} type="password" placeholder={`Your ${product.name} PAT`} value={patToken} onChange={(e) => setPatToken(e.target.value)} required className="h-9" />
                </div>
                <Button type="submit" variant="outline" disabled={isPatConnecting} className="w-full">
                  {isPatConnecting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  Connect
                </Button>
              </form>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
