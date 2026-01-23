"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/hooks/use-toast";
import { useUserId } from "@/hooks/use-user-id";
import { Loader2, Plus, Copy, Check, Trash2, ShieldCheck } from "lucide-react";

type ManagedSshKey = {
  id: number;
  provider: string;
  label?: string | null;
  publicKey?: string | null;
  createdAt?: string | null;
  hasSecret: boolean;
};

type ConnectionTestPayload = {
  ipAddress: string;
  port: string;
  username: string;
  sshKeyId: string;
  sshJumpCommand: string;
};

// API calls go through Next.js API routes to avoid CORS issues

export function SSHKeyManager() {
  const { toast } = useToast();
  const { userId, isLoading: userLoading } = useUserId();

  const [keys, setKeys] = useState<ManagedSshKey[]>([]);
  const [isCreating, setIsCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [copiedKeyId, setCopiedKeyId] = useState<number | null>(null);
  const [isTesting, setIsTesting] = useState(false);
  const [nameEdits, setNameEdits] = useState<Record<number, string>>({});
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [revealedKeyId, setRevealedKeyId] = useState<number | null>(null);
  const [testPayload, setTestPayload] = useState<ConnectionTestPayload>({
    ipAddress: "",
    port: "22",
    username: "",
    sshKeyId: "",
    sshJumpCommand: "",
  });

  const disabled = userLoading || !userId;

  const fetchKeys = async () => {
    if (disabled) return;
    try {
      const res = await fetch(`/api/ssh-keys`, {
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to load SSH keys");
      const data = await res.json();
      const loaded = data.keys || [];
      setKeys(loaded);
      setNameEdits(
        loaded.reduce(
          (acc: Record<number, string>, key: ManagedSshKey) => ({
            ...acc,
            [key.id]: key.label || key.provider,
          }),
          {},
        ),
      );
    } catch (err) {
      console.error(err);
      toast({
        title: "Failed to load keys",
        description: "Could not load SSH keys. Please try again.",
        variant: "destructive",
      });
    }
  };

  const toggleKeyReveal = (keyId: number) => {
    setRevealedKeyId((prev) => (prev === keyId ? null : keyId));
  };

  const handleGenerate = async () => {
    if (disabled) return;
    setIsCreating(true);
    try {
      const res = await fetch(`/api/ssh-keys`, {
        method: "POST",
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Failed to create SSH key");
      }
      toast({
        title: "Key generated",
        description: "Public key copied below. Add it to your VM(s).",
      });
      await fetchKeys();
      // Preselect the new key for connection testing
      if (data.id) {
        setTestPayload((prev) => ({ ...prev, sshKeyId: String(data.id) }));
      }
    } catch (err) {
      console.error(err);
      toast({
        title: "Key generation failed",
        description:
          err instanceof Error ? err.message : "Could not create SSH key.",
        variant: "destructive",
      });
    } finally {
      setIsCreating(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (disabled) return;
    setDeletingId(id);
    try {
      const res = await fetch(`/api/ssh-keys/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Failed to delete SSH key");
      }
      toast({
        title: "Key deleted",
        description: "SSH key removed successfully.",
      });
      await fetchKeys();
    } catch (err) {
      console.error(err);
      toast({
        title: "Delete failed",
        description:
          err instanceof Error ? err.message : "Could not delete SSH key.",
        variant: "destructive",
      });
    } finally {
      setDeletingId(null);
    }
  };

  const handleCopy = async (key: ManagedSshKey) => {
    if (!key.publicKey) {
      toast({
        title: "No public key",
        description: "This key has no public material to copy.",
        variant: "destructive",
      });
      return;
    }
    try {
      await navigator.clipboard.writeText(key.publicKey);
      setCopiedKeyId(key.id);
      toast({
        title: "Copied",
        description: "Public key copied to clipboard.",
      });
      setTimeout(() => setCopiedKeyId(null), 1500);
    } catch (err) {
      console.error(err);
      toast({
        title: "Copy failed",
        description: "Unable to copy to clipboard.",
        variant: "destructive",
      });
    }
  };

  const handleRename = async (keyId: number) => {
    if (disabled) return;
    const label = (nameEdits[keyId] || "").trim();
    if (!label) {
      toast({
        title: "Label required",
        description: "Please provide a name for the key.",
        variant: "destructive",
      });
      return;
    }

    setRenamingId(keyId);
    try {
      const res = await fetch(`/api/ssh-keys/${keyId}`, {
        method: "PATCH",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ label }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Failed to rename SSH key");
      }
      toast({ title: "Renamed", description: "SSH key renamed successfully." });
      await fetchKeys();
    } catch (err) {
      console.error(err);
      toast({
        title: "Rename failed",
        description:
          err instanceof Error ? err.message : "Could not rename SSH key.",
        variant: "destructive",
      });
    } finally {
      setRenamingId(null);
    }
  };

  const handleTest = async () => {
    if (disabled) return;
    const { ipAddress, port, username, sshKeyId, sshJumpCommand } = testPayload;
    if (!ipAddress || !port || !username || !sshKeyId) {
      toast({
        title: "Missing fields",
        description: "IP, port, username, and SSH key are required.",
        variant: "destructive",
      });
      return;
    }
    setIsTesting(true);
    try {
      const res = await fetch(`/api/vms/check-connection`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ipAddress,
          port: Number(port),
          username,
          sshKeyId: Number(sshKeyId),
          sshJumpCommand: sshJumpCommand || null,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.success === false) {
        throw new Error(data.error || "SSH connection failed");
      }
      toast({
        title: "Connection successful",
        description: data.connectedAs
          ? `Connected as ${data.connectedAs}`
          : "SSH check passed.",
      });
    } catch (err) {
      console.error(err);
      toast({
        title: "Connection failed",
        description:
          err instanceof Error ? err.message : "SSH validation failed.",
        variant: "destructive",
      });
    } finally {
      setIsTesting(false);
    }
  };

  useEffect(() => {
    fetchKeys();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const sortedKeys = useMemo(
    () =>
      [...keys].sort((a, b) => {
        const aTime = a.createdAt ? new Date(a.createdAt).getTime() : 0;
        const bTime = b.createdAt ? new Date(b.createdAt).getTime() : 0;
        return bTime - aTime;
      }),
    [keys],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-6">
        <div className="flex-1">
          <h1 className="text-2xl font-semibold">SSH Keys</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Generate and manage Aurora-managed SSH keys for bastions and VMs.
            Public keys are safe to share; private keys are stored securely in
            Vault.
          </p>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          <Button onClick={handleGenerate} disabled={isCreating || disabled}>
            {isCreating ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Plus className="mr-2 h-4 w-4" />
            )}
            Generate Key
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="space-y-3 pt-6">
          {sortedKeys.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No SSH keys yet. Generate one to get started.
            </p>
          )}
          {sortedKeys.map((key) => {
            const isRevealed = revealedKeyId === key.id;

            return (
              <div
                key={key.id}
                className="flex flex-col gap-2 rounded-lg border p-3 md:flex-row md:items-center md:justify-between"
              >
                <div className="space-y-1">
                  <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        value={nameEdits[key.id] ?? key.label ?? key.provider}
                        onChange={(e) =>
                          setNameEdits((prev) => ({
                            ...prev,
                            [key.id]: e.target.value,
                          }))
                        }
                        placeholder="Key label"
                        aria-label="Key label"
                        className="h-8 w-full min-w-[160px] max-w-[280px] bg-transparent px-2 text-base font-semibold md:w-auto"
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleRename(key.id)}
                        disabled={
                          renamingId === key.id || !nameEdits[key.id]?.trim()
                        }
                      >
                        {renamingId === key.id ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Renaming...
                          </>
                        ) : (
                          "Rename"
                        )}
                      </Button>
                      <Badge variant="outline">{key.provider}</Badge>
                      {key.hasSecret ? (
                        <Badge
                          className="bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-200"
                          variant="secondary"
                        >
                          <ShieldCheck className="mr-1 h-3 w-3" />
                          Stored
                        </Badge>
                      ) : (
                        <Badge variant="destructive">Missing secret</Badge>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {key.createdAt
                        ? new Date(key.createdAt).toLocaleString()
                        : "Created date unknown"}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => toggleKeyReveal(key.id)}
                    disabled={!key.publicKey}
                    className={`w-full rounded border border-white/10 bg-black px-2 py-1 text-left text-xs font-mono text-white/80 hover:border-white/20 disabled:cursor-not-allowed disabled:opacity-60 sm:max-w-[260px] ${
                      isRevealed
                        ? "overflow-x-auto whitespace-nowrap"
                        : "truncate"
                    }`}
                    title={
                      key.publicKey
                        ? isRevealed
                          ? "Click to hide public key"
                          : "Click to reveal public key"
                        : "No public key available"
                    }
                    aria-pressed={key.publicKey ? isRevealed : undefined}
                  >
                    {key.publicKey
                      ? isRevealed
                        ? key.publicKey
                        : "********************"
                      : "No public key available"}
                  </button>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleCopy(key)}
                  >
                    {copiedKeyId === key.id ? (
                      <Check className="mr-2 h-4 w-4" />
                    ) : (
                      <Copy className="mr-2 h-4 w-4" />
                    )}
                    {copiedKeyId === key.id ? "Copied" : "Copy"}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleDelete(key.id)}
                    disabled={deletingId === key.id}
                    className="text-red-600"
                  >
                    {deletingId === key.id ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Deleting...
                      </>
                    ) : (
                      <>
                        <Trash2 className="mr-2 h-4 w-4" />
                        Delete
                      </>
                    )}
                  </Button>
                </div>
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Check Connection</CardTitle>
          <CardDescription>
            Validate bastion or direct SSH access using a managed key.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="ip">Target IP / Hostname</Label>
              <Input
                id="ip"
                placeholder="1.2.3.4 or host"
                value={testPayload.ipAddress}
                onChange={(e) =>
                  setTestPayload((p) => ({ ...p, ipAddress: e.target.value }))
                }
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="port">Port</Label>
              <Input
                id="port"
                type="number"
                min={1}
                max={65535}
                value={testPayload.port}
                onChange={(e) =>
                  setTestPayload((p) => ({ ...p, port: e.target.value }))
                }
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="username">SSH Username</Label>
              <Input
                id="username"
                placeholder="ubuntu, debian, root, etc."
                value={testPayload.username}
                onChange={(e) =>
                  setTestPayload((p) => ({ ...p, username: e.target.value }))
                }
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="sshKeyId">SSH Key</Label>
              <select
                id="sshKeyId"
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                value={testPayload.sshKeyId}
                onChange={(e) =>
                  setTestPayload((p) => ({ ...p, sshKeyId: e.target.value }))
                }
              >
                <option value="">Select a key</option>
                {sortedKeys.map((key) => (
                  <option key={key.id} value={key.id}>
                    {key.label || key.provider}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="jump">SSH Jump Command (optional)</Label>
            <Textarea
              id="jump"
              placeholder="ssh -J bastion -p 2200 user@target"
              value={testPayload.sshJumpCommand}
              onChange={(e) =>
                setTestPayload((p) => ({
                  ...p,
                  sshJumpCommand: e.target.value,
                }))
              }
            />
          </div>
          <div className="flex justify-end">
            <Button onClick={handleTest} disabled={isTesting || disabled}>
              {isTesting ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <ShieldCheck className="mr-2 h-4 w-4" />
              )}
              {isTesting ? "Testing..." : "Check Connection"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
