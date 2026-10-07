"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronRight, Loader2, Plug, RefreshCw, Trash2 } from "lucide-react";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { getUserFriendlyError } from "@/lib/utils";
import {
  mcpService, McpDetectResult, McpRegisterPayload, McpServerSummary, McpToolMode,
} from "@/lib/services/mcp";

const EMPTY_FORM: McpRegisterPayload = {
  label: "",
  url: "",
  authType: "bearer",
  token: "",
  headerName: "",
};

/** Give up if the user abandons the consent screen, so the form unlocks. */
const OAUTH_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * Resolve with the authorization code the callback page posts back.
 *
 * Rejects on provider error, a closed popup, or timeout -- otherwise an
 * abandoned flow would leave the submit button spinning forever.
 */
function waitForOAuthCode(popup: Window, expectedState: string): Promise<{ code: string }> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.removeEventListener("message", onMessage);
      window.clearInterval(closedTimer);
      window.clearTimeout(timeout);
    };

    const onMessage = (event: MessageEvent) => {
      // Only trust our own callback page; the popup visits the provider's
      // origin too and anything could be loaded in between.
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; code?: string; state?: string; error?: string; errorDescription?: string };
      if (data?.type === "mcp-auth-error") {
        cleanup();
        reject(new Error(data.errorDescription || data.error || "Authorization was denied."));
        return;
      }
      if (data?.type !== "mcp-auth-success" || !data.code) return;
      // State must match the value we started with, or this is a different flow.
      if (data.state !== expectedState) return;
      cleanup();
      resolve({ code: data.code });
    };

    const closedTimer = window.setInterval(() => {
      if (popup.closed) {
        cleanup();
        reject(new Error("The authorization window was closed before finishing."));
      }
    }, 500);

    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error("Authorization timed out. Please try again."));
    }, OAUTH_TIMEOUT_MS);

    window.addEventListener("message", onMessage);
  });
}

export default function McpAuthPage() {
  const { toast } = useToast();
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [maxServers, setMaxServers] = useState(10);
  const [form, setForm] = useState<McpRegisterPayload>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  // Named rather than a bare boolean so the button can say which of the three
  // waits is happening -- they take seconds each and look identical otherwise.
  const [phase, setPhase] = useState<"idle" | "checking" | "signin" | "connecting">("idle");
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  // What the probe found this URL needs; null until it runs or the URL changes.
  const [detected, setDetected] = useState<McpDetectResult["authType"] | null>(null);
  const [manual, setManual] = useState(false);
  const submitting = phase !== "idle";

  const load = useCallback(async () => {
    try {
      const data = await mcpService.listServers();
      setServers(data.servers);
      setMaxServers(data.maxServers);
    } catch (error: unknown) {
      toast({
        title: "Could not load MCP servers",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);

  const handleRegister = async (payload: McpRegisterPayload = form) => {
    setPhase("connecting");
    try {
      const { server, warning } = await mcpService.register(payload);
      toast({
        title: `Connected to ${server.label}`,
        description: warning
          ? `${warning} ${server.toolCount} available.`
          : `Aurora discovered ${server.toolCount} tool${server.toolCount === 1 ? "" : "s"}.`,
      });
      setForm(EMPTY_FORM);
      setDetected(null);
      setManual(false);
      await load();
    } catch (error: unknown) {
      // The backend only stores a server after a successful handshake, so this
      // is the user's single chance to see why the connection failed.
      toast({
        title: "Could not connect",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setPhase("idle");
    }
  };

  /**
   * Run the OAuth flow in a popup.
   *
   * Aurora registers itself with the provider (Dynamic Client Registration),
   * the user consents in the popup, and the callback page posts the code back.
   * The code is exchanged server-side, where the PKCE verifier lives.
   *
   * The popup is opened after startOAuth returns, which is not synchronous
   * with the click. Chrome allows that; Safari may block it, and the thrown
   * message below is what the user gets if it does. Opening it up front
   * instead means showing a blank window for the several seconds that
   * discovery and client registration take, which is worse for every server.
   */
  const handleOAuth = async () => {
    setPhase("signin");
    let popup: Window | null = null;
    try {
      const { authorizeUrl, state } = await mcpService.startOAuth(form);
      popup = window.open(authorizeUrl, "mcp-oauth", "width=600,height=760");
      if (!popup) {
        throw new Error("Allow pop-ups for this site to authorize the server.");
      }

      const { code } = await waitForOAuthCode(popup, state);
      // Consent is done and the popup has closed itself; what follows is the
      // token exchange plus tool discovery, which is not a wait on the user.
      setPhase("connecting");
      const { server, warning } = await mcpService.completeOAuth(code, state);
      toast({
        title: `Connected to ${server.label}`,
        description: warning
          ? `${warning} ${server.toolCount} available.`
          : `Aurora discovered ${server.toolCount} tool${server.toolCount === 1 ? "" : "s"}.`,
      });
      setForm(EMPTY_FORM);
      setDetected(null);
      setManual(false);
      await load();
    } catch (error: unknown) {
      toast({
        title: "Could not connect",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      popup?.close();
      setPhase("idle");
    }
  };

  const handleRefresh = async (label: string) => {
    setBusyLabel(label);
    try {
      const { server, warning } = await mcpService.refresh(label);
      toast({
        title: `${label} refreshed`,
        description: warning ?? `${server.toolCount} tool${server.toolCount === 1 ? "" : "s"} available.`,
      });
      await load();
    } catch (error: unknown) {
      toast({
        title: `Could not refresh ${label}`,
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setBusyLabel(null);
    }
  };

  /**
   * Persist one tool's override, updating the badge before the request lands so
   * the dropdown does not snap back while the PATCH is in flight.
   */
  const handleToolMode = async (label: string, toolName: string, mode: McpToolMode) => {
    setServers((prev) => prev.map((s) => s.label !== label ? s : {
      ...s,
      tools: s.tools.map((t) => (t.name === toolName ? { ...t, mode } : t)),
    }));
    try {
      await mcpService.setToolMode(label, toolName, mode);
    } catch (error: unknown) {
      toast({
        title: `Could not update ${toolName}`,
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
      await load();
    }
  };

  const handleRemove = async (label: string) => {
    setBusyLabel(label);
    try {
      await mcpService.remove(label);
      toast({ title: `Removed ${label}` });
      await load();
    } catch (error: unknown) {
      toast({
        title: `Could not remove ${label}`,
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setBusyLabel(null);
    }
  };

  const isOAuth = form.authType === "oauth";
  // OAuth collects its credential in the popup, not the form. Before detection
  // has run there is nothing to ask for yet, so the field stays hidden.
  const needsToken = (manual || detected === "token") && form.authType !== "none" && !isOAuth;
  const atCapacity = servers.length >= maxServers;
  // Before detection runs there is nothing to validate beyond name and URL:
  // which credential fields matter is not known yet.
  const canSubmit =
    Boolean(form.label.trim()) &&
    Boolean(form.url.trim()) &&
    (!needsToken || Boolean(form.token?.trim())) &&
    (form.authType !== "header" || Boolean(form.headerName?.trim())) &&
    !submitting &&
    !atCapacity;

  /**
   * Detect what the URL needs, then connect in the same click.
   *
   * Users cannot be expected to know whether a URL is OAuth, an API key, or
   * open, so the first thing this does is probe. Detection used to stop here
   * and make the user press Connect again, which read as a dead button: the
   * spinner ran for the length of the probe and then nothing visibly happened,
   * because the only change was a hidden state flag.
   *
   * So it now continues straight into the connect it already has enough
   * information to perform. It can only stop when it genuinely needs something
   * from the user: a token it does not have.
   */
  const handleSubmit = async () => {
    if (manual || detected) {
      return isOAuth ? handleOAuth() : handleRegister();
    }

    setPhase("checking");
    try {
      const { authType } = await mcpService.detect(form.url.trim());
      setDetected(authType);

      if (authType === "oauth") {
        setForm((f) => ({ ...f, authType: "oauth" }));
        return await handleOAuth();
      }

      if (authType === "none") {
        // Nothing to collect, so saving is the obvious next step. The form
        // state update is async, hence passing the payload explicitly.
        const payload = { ...form, authType: "none" as const };
        setForm(payload);
        return await handleRegister(payload);
      }
      // Needs a token we do not have. This is the one case where stopping is
      // correct, and the field appearing makes that visible.
      setForm((f) => ({ ...f, authType: "bearer" }));
    } catch (error: unknown) {
      toast({
        title: "Could not reach the server",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      // handleOAuth and handleRegister own the phase once handed to them.
      setPhase((p) => (p === "checking" ? "idle" : p));
    }
  };

  return (
    <ConnectorAuthGuard connectorName="Custom MCP Servers">
      <div className="container mx-auto py-8 px-4 max-w-4xl space-y-6">
        <div>
          <h1 className="text-3xl font-bold">Custom MCP Servers</h1>
          <p className="text-muted-foreground mt-1">
            Register your own MCP servers so Aurora can query systems it has no built-in
            connector for. Aurora discovers each server&apos;s tools on connect and uses them
            during chat and investigations.
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Add a server</CardTitle>
            <CardDescription>
              Aurora connects to the server now and only saves it if the handshake succeeds.
              Remote HTTP servers only &mdash; stdio servers are not supported.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="mcp-label">Name</Label>
              <Input
                id="mcp-label"
                placeholder="netbox"
                value={form.label}
                onChange={(e) => setForm({ ...form, label: e.target.value })}
              />
              <p className="text-xs text-muted-foreground">
                How you and the agent refer to this server when picking a tool.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="mcp-url">Server URL</Label>
              <Input
                id="mcp-url"
                placeholder="https://mcp.example.com/mcp"
                value={form.url}
                onChange={(e) => { setForm({ ...form, url: e.target.value }); setDetected(null); }}
              />
              <p className="text-xs text-muted-foreground">
                Aurora works out the transport and what credentials the server needs.
              </p>
            </div>

            {detected === "token" && (
              <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                This server requires credentials. Paste its token below to connect.
              </p>
            )}

            {isOAuth && (
              <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                This server uses OAuth. Aurora will open its sign-in page in a pop-up and
                register itself automatically &mdash; no token to paste.
              </p>
            )}

            {needsToken && (
              <div className="space-y-2">
                <Label htmlFor="mcp-token">Token</Label>
                <Input
                  id="mcp-token"
                  type="password"
                  autoComplete="off"
                  placeholder="Paste the server's access token"
                  value={form.token}
                  onChange={(e) => setForm({ ...form, token: e.target.value })}
                />
                <p className="text-xs text-muted-foreground">
                  Stored encrypted in Vault and sent only to this server.
                </p>
              </div>
            )}

            {/* Escape hatch: detection cannot tell "needs auth" from "forbidden"
                when a server answers 403, and a server with broken dynamic client
                registration needs a hand-entered client ID. */}
            {manual ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="mcp-auth">Authentication</Label>
                  <Select
                    value={form.authType}
                    onValueChange={(v) =>
                      setForm({ ...form, authType: v as McpRegisterPayload["authType"] })
                    }
                  >
                    <SelectTrigger id="mcp-auth">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="oauth">OAuth (sign in)</SelectItem>
                      <SelectItem value="bearer">Bearer token</SelectItem>
                      <SelectItem value="header">Custom header</SelectItem>
                      <SelectItem value="none">None</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                {form.authType === "header" && (
                  <div className="space-y-2">
                    <Label htmlFor="mcp-header">Header name</Label>
                    <Input
                      id="mcp-header"
                      placeholder="X-Api-Key"
                      value={form.headerName}
                      onChange={(e) => setForm({ ...form, headerName: e.target.value })}
                    />
                  </div>
                )}
              </div>
            ) : (
              <button
                type="button"
                className="text-xs text-muted-foreground underline"
                onClick={() => setManual(true)}
              >
                Set authentication manually
              </button>
            )}

            <p className="rounded-md border p-3 text-xs text-muted-foreground">
              Aurora cannot tell what a third-party tool changes, so tools that do not look
              like reads default to asking you for confirmation before they run, which also
              keeps them out of automated investigations. You can set this per tool once the
              server is connected.
            </p>

            {atCapacity && (
              <p className="text-sm text-destructive">
                You have reached the limit of {maxServers} servers. Remove one to add another.
              </p>
            )}

            <Button onClick={handleSubmit} disabled={!canSubmit}>
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  {phase === "checking"
                    ? "Checking server\u2026"
                    : phase === "signin"
                      ? "Waiting for sign-in\u2026"
                      : "Discovering tools\u2026"}
                </>
              ) : (
                <>
                  <Plug className="mr-2 h-4 w-4" />
                  {isOAuth ? "Sign in and connect" : "Connect"}
                </>
              )}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Connected servers</CardTitle>
            <CardDescription>
              {servers.length} of {maxServers} registered.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading&hellip;
              </div>
            ) : servers.length === 0 ? (
              <p className="text-sm text-muted-foreground">No MCP servers registered yet.</p>
            ) : (
              <ul className="space-y-4">
                {servers.map((server) => (
                  <li key={server.label} className="rounded-md border p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{server.label}</span>
                          {/* Tool count lives on the expander below, which is
                              where it is actionable. */}
                          <Badge variant="outline" className="text-xs font-normal">
                            {server.authType === "none" ? "no auth" : server.authType}
                          </Badge>
                        </div>
                        <p className="mt-1 truncate text-xs text-muted-foreground">
                          {server.url}
                        </p>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busyLabel === server.label}
                          onClick={() => handleRefresh(server.label)}
                        >
                          {busyLabel === server.label ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <RefreshCw className="h-4 w-4" />
                          )}
                          <span className="ml-2">Refresh tools</span>
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={busyLabel === server.label}
                          onClick={() => handleRemove(server.label)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                    {server.tools.length > 0 && (
                      <Collapsible className="mt-3">
                        <CollapsibleTrigger className="group flex w-full items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
                          <ChevronRight className="h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-90" />
                          {/* Collapsed by default: a 76-tool server made the card
                              unscrollable, and the per-tool selects are a rare edit.
                              The counts are the part worth seeing at a glance. */}
                          <span>
                            {server.tools.length} tools
                            {(() => {
                              const confirm = server.tools.filter((t) => t.mode === "confirm").length;
                              return confirm ? ` · ${confirm} need confirmation` : "";
                            })()}
                          </span>
                        </CollapsibleTrigger>
                        <CollapsibleContent className="mt-2 space-y-1.5">
                          {server.tools.map((tool) => (
                            <div key={tool.name} className="flex items-center gap-2">
                              <Badge
                                variant={tool.write ? "destructive" : "secondary"}
                                className="font-mono text-xs font-normal"
                                title={
                                  `${tool.write ? "Write tool" : "Read-only tool"} — ` +
                                  `${tool.declared ? "declared by the server" : "inferred from its name"}`
                                }
                              >
                                {tool.name}
                              </Badge>
                              <Select
                                value={tool.mode}
                                onValueChange={(mode) =>
                                  handleToolMode(server.label, tool.name, mode as McpToolMode)
                                }
                              >
                                <SelectTrigger className="h-7 w-[150px] text-xs">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="allow">Allow</SelectItem>
                                  <SelectItem value="confirm">Confirm</SelectItem>
                                </SelectContent>
                              </Select>
                            </div>
                          ))}
                          <p className="pt-1 text-xs text-muted-foreground">
                            <span className="font-medium">Allow</span> runs without asking,
                            including during automated investigations.{" "}
                            <span className="font-medium">Confirm</span> asks you first, so it
                            is skipped when no one is there to answer. Writes default to
                            Confirm, reads to Allow.
                          </p>
                        </CollapsibleContent>
                      </Collapsible>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </ConnectorAuthGuard>
  );
}
