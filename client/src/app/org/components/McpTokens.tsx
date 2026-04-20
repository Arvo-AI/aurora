"use client";

import { useState, useEffect, useCallback } from "react";
import { Plus, Trash2, Copy, Loader2, Key, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { toast } from "@/hooks/use-toast";
import { getEnv } from "@/lib/env";

interface McpToken {
  id: number;
  name: string;
  token_preview: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  status: string;
}

export default function McpTokens() {
  const [tokens, setTokens] = useState<McpToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newTokenName, setNewTokenName] = useState("");
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [revoking, setRevoking] = useState<number | null>(null);
  const [copiedToken, setCopiedToken] = useState(false);
  const [copiedConfig, setCopiedConfig] = useState(false);

  const fetchTokens = useCallback(async () => {
    try {
      const res = await fetch("/api/mcp/tokens");
      if (res.ok) {
        const data = await res.json();
        setTokens(data.tokens || []);
      }
    } catch {
      toast({ title: "Failed to load tokens", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchTokens(); }, [fetchTokens]);

  async function createToken() {
    if (!newTokenName.trim()) return;
    setCreating(true);
    try {
      const res = await fetch("/api/mcp/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newTokenName.trim() }),
      });
      const data = await res.json();
      if (res.ok && data.token) {
        setCreatedToken(data.token);
        fetchTokens();
      } else {
        toast({ title: data.error || "Failed to create token", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to create token", variant: "destructive" });
    } finally {
      setCreating(false);
    }
  }

  async function revokeToken(id: number) {
    setRevoking(id);
    try {
      const res = await fetch(`/api/mcp/tokens/${id}`, { method: "DELETE" });
      if (res.ok) {
        toast({ title: "Token revoked" });
        fetchTokens();
      } else {
        toast({ title: "Failed to revoke token", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to revoke token", variant: "destructive" });
    } finally {
      setRevoking(null);
    }
  }

  function copyToken() {
    if (createdToken) {
      navigator.clipboard.writeText(createdToken);
      setCopiedToken(true);
      setTimeout(() => setCopiedToken(false), 2000);
    }
  }

  function mcpConfigJson(token: string) {
    const backendUrl = (getEnv('NEXT_PUBLIC_BACKEND_URL') || 'http://localhost:5080').replace(/\/$/, '');
    return JSON.stringify({
      mcpServers: {
        aurora: {
          url: `${backendUrl}/mcp`,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    }, null, 2);
  }

  function copyConfig(token: string) {
    navigator.clipboard.writeText(mcpConfigJson(token));
    setCopiedConfig(true);
    setTimeout(() => setCopiedConfig(false), 2000);
  }

  function handleDialogClose(open: boolean) {
    setDialogOpen(open);
    if (!open) {
      setNewTokenName("");
      setCreatedToken(null);
      setCopiedToken(false);
      setCopiedConfig(false);
    }
  }

  function formatDate(iso: string | null) {
    if (!iso) return "--";
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading tokens...
      </div>
    );
  }

  return (
    <div className="space-y-4 mr-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium">MCP API Tokens</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            Generate tokens for MCP clients (Claude Desktop, Cursor, etc.) to access Aurora
          </p>
        </div>
        <Dialog open={dialogOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm" className="gap-1.5">
              <Plus className="h-3.5 w-3.5" />
              Generate Token
            </Button>
          </DialogTrigger>
          <DialogContent className="sm:max-w-2xl overflow-hidden">
            {createdToken ? (
              <>
                <DialogHeader>
                  <DialogTitle>Token Created</DialogTitle>
                  <DialogDescription>
                    Copy the configuration below into your MCP client. The token will not be shown again.
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-3">
                  <div className="flex gap-2 min-w-0">
                    <Input
                      readOnly
                      value={createdToken}
                      className="font-mono !text-[11px] min-w-0"
                    />
                    <Button size="icon" variant="outline" onClick={copyToken} title="Copy token">
                      {copiedToken ? <Check className="h-4 w-4 text-emerald-500" /> : <Copy className="h-4 w-4" />}
                    </Button>
                  </div>
                  <div className="relative">
                    <pre className="bg-muted rounded-md p-3 pr-10 text-xs font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-48">{mcpConfigJson(createdToken)}</pre>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="absolute top-1.5 right-1.5 h-7 w-7"
                      onClick={() => copyConfig(createdToken)}
                    >
                      {copiedConfig ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Paste into <code className="text-[11px] bg-muted px-1 py-0.5 rounded">.cursor/mcp.json</code> (Cursor) or <code className="text-[11px] bg-muted px-1 py-0.5 rounded">claude_desktop_config.json</code> (Claude Desktop). Works with any MCP client.
                  </p>
                </div>
                <DialogFooter>
                  <Button onClick={() => handleDialogClose(false)}>Done</Button>
                </DialogFooter>
              </>
            ) : (
              <>
                <DialogHeader>
                  <DialogTitle>Generate MCP Token</DialogTitle>
                  <DialogDescription>
                    Give this token a name to identify where it is used.
                  </DialogDescription>
                </DialogHeader>
                <Input
                  placeholder="e.g. My Cursor IDE"
                  value={newTokenName}
                  onChange={(e) => setNewTokenName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") createToken(); }}
                  autoFocus
                />
                <DialogFooter>
                  <Button variant="outline" onClick={() => handleDialogClose(false)}>Cancel</Button>
                  <Button onClick={createToken} disabled={creating || !newTokenName.trim()}>
                    {creating && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                    Generate
                  </Button>
                </DialogFooter>
              </>
            )}
          </DialogContent>
        </Dialog>
      </div>

      {tokens.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Key className="h-8 w-8 mb-2 opacity-40" />
          <p className="text-sm">No API tokens yet</p>
          <p className="text-xs mt-1">Generate a token to connect MCP clients to Aurora</p>
        </div>
      ) : (
        <div className="border rounded-lg divide-y">
          {tokens.map((t) => (
            <div key={t.id} className="flex items-center justify-between px-4 py-3 text-sm">
              <div className="flex flex-col gap-0.5 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium truncate">{t.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                    t.status === "active"
                      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                      : "bg-red-500/10 text-red-600 dark:text-red-400"
                  }`}>
                    {t.status}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span className="font-mono">{t.token_preview}</span>
                  <span>Created {formatDate(t.created_at)}</span>
                  {t.last_used_at && <span>Last used {formatDate(t.last_used_at)}</span>}
                  {t.expires_at && <span>Expires {formatDate(t.expires_at)}</span>}
                </div>
              </div>
              {t.status === "active" && (
                <Button
                  size="icon"
                  variant="ghost"
                  className="h-8 w-8 text-muted-foreground hover:text-red-600"
                  onClick={() => revokeToken(t.id)}
                  disabled={revoking === t.id}
                >
                  {revoking === t.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
