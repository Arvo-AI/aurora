"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Plug, RefreshCw, Trash2 } from "lucide-react";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { getUserFriendlyError } from "@/lib/utils";
import {
  mcpService, McpRegisterPayload, McpServerSummary,
} from "@/lib/services/mcp";

const EMPTY_FORM: McpRegisterPayload = {
  label: "",
  url: "",
  transport: "streamable_http",
  authType: "bearer",
  token: "",
  headerName: "",
  readOnly: true,
};

export default function McpAuthPage() {
  const { toast } = useToast();
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [maxServers, setMaxServers] = useState(10);
  const [form, setForm] = useState<McpRegisterPayload>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);

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

  const handleRegister = async () => {
    setSubmitting(true);
    try {
      const { server, warning } = await mcpService.register(form);
      toast({
        title: `Connected to ${server.label}`,
        description: warning
          ? `${warning} ${server.toolCount} available.`
          : `Aurora discovered ${server.toolCount} tool${server.toolCount === 1 ? "" : "s"}.`,
      });
      setForm(EMPTY_FORM);
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
      setSubmitting(false);
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

  const needsToken = form.authType !== "none";
  const atCapacity = servers.length >= maxServers;
  const canSubmit =
    Boolean(form.label.trim()) &&
    Boolean(form.url.trim()) &&
    (!needsToken || Boolean(form.token?.trim())) &&
    (form.authType !== "header" || Boolean(form.headerName?.trim())) &&
    !submitting &&
    !atCapacity;

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
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="mcp-label">Name</Label>
                <Input
                  id="mcp-label"
                  placeholder="netbox"
                  value={form.label}
                  onChange={(e) => setForm({ ...form, label: e.target.value })}
                />
                <p className="text-xs text-muted-foreground">
                  Used to prefix the server&apos;s tools so Aurora can tell them apart.
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="mcp-transport">Transport</Label>
                <Select
                  value={form.transport}
                  onValueChange={(v) =>
                    setForm({ ...form, transport: v as McpRegisterPayload["transport"] })
                  }
                >
                  <SelectTrigger id="mcp-transport">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="streamable_http">Streamable HTTP</SelectItem>
                    <SelectItem value="sse">HTTP + SSE (legacy)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="mcp-url">Server URL</Label>
              <Input
                id="mcp-url"
                placeholder="https://mcp.example.com/mcp"
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
              />
            </div>

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

            <div className="flex items-start gap-3 rounded-md border p-3">
              <Checkbox
                id="mcp-readonly"
                checked={form.readOnly}
                onCheckedChange={(checked) => setForm({ ...form, readOnly: checked === true })}
              />
              <div className="space-y-1">
                <Label htmlFor="mcp-readonly" className="font-medium">
                  Register read-only tools only
                </Label>
                <p className="text-xs text-muted-foreground">
                  Recommended. Aurora cannot tell what a third-party tool changes, so any
                  tool that is not clearly a read is treated as a write: it needs your
                  confirmation in chat and is unavailable during automated investigations.
                </p>
              </div>
            </div>

            {atCapacity && (
              <p className="text-sm text-destructive">
                You have reached the limit of {maxServers} servers. Remove one to add another.
              </p>
            )}

            <Button onClick={handleRegister} disabled={!canSubmit}>
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Connecting&hellip;
                </>
              ) : (
                <>
                  <Plug className="mr-2 h-4 w-4" />
                  Connect
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
                          <Badge variant="secondary">{server.toolCount} tools</Badge>
                          {server.readOnly && <Badge variant="outline">read-only</Badge>}
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
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {server.tools.map((tool) => (
                          <Badge
                            key={tool.name}
                            variant={tool.write ? "destructive" : "secondary"}
                            className="font-mono text-xs font-normal"
                            title={tool.write ? "Write tool — requires confirmation" : "Read-only tool"}
                          >
                            {tool.name}
                          </Badge>
                        ))}
                      </div>
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
