"use client";

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { ArrowLeft, Loader2, LogOut, RefreshCw, Copy, Check, Shield, Key, Terminal, Trash2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { copyToClipboard } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/utils/time-format";
import { providerPreferencesService } from "@/lib/services/providerPreferences";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

interface TailscaleDevice {
  id: string;
  hostname: string;
  name: string;
  addresses: string[];
  authorized: boolean;
  blocked: boolean;
  tags: string[];
  lastSeen: string;
  os: string;
  clientVersion: string;
  updateAvailable: boolean;
  user: string;
  created: string;
  expires: string;
}

interface SSHSetupData {
  sshPublicKey: string;
  command: string;
}

function isDeviceOnline(lastSeen: string): boolean {
  try {
    const diffMinutes = (Date.now() - new Date(lastSeen).getTime()) / 60000;
    return diffMinutes < 5;
  } catch {
    return false;
  }
}

export default function ManageTailscalePage() {
  const router = useRouter();
  const { toast } = useToast();

  const [devices, setDevices] = useState<TailscaleDevice[]>([]);
  const [tailnetName, setTailnetName] = useState("");
  const [sshData, setSSHData] = useState<SSHSetupData | null>(null);
  const [loadingDevices, setLoadingDevices] = useState(true);
  const [loadingSSH, setLoadingSSH] = useState(true);
  const [disconnecting, setDisconnecting] = useState(false);
  const [authorizingDevice, setAuthorizingDevice] = useState<string | null>(null);
  const [removingDevice, setRemovingDevice] = useState<string | null>(null);
  const [showDisconnectConfirm, setShowDisconnectConfirm] = useState(false);
  const [deviceToRemove, setDeviceToRemove] = useState<{ id: string; hostname: string } | null>(null);
  const [copied, setCopied] = useState<"key" | "command" | null>(null);
  const copyTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    const init = async () => {
      try {
        const res = await fetch("/api/tailscale/status");
        if (!res.ok) {
          console.error("Tailscale status check failed:", res.status);
          setLoadingDevices(false);
          setLoadingSSH(false);
          return;
        }
        const data = await res.json();
        setTailnetName(data.tailnetName || data.tailnet || "");
        if (!data.connected) {
          router.push("/tailscale/onboarding");
          return;
        }
      } catch (error) {
        console.error("Error loading Tailscale status:", error);
        setLoadingDevices(false);
        setLoadingSSH(false);
        return;
      }
      loadDevices();
      loadSSHSetup();
    };
    init();
    return () => {
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadDevices = async () => {
    try {
      setLoadingDevices(true);
      const res = await fetch("/api/tailscale/devices");
      if (!res.ok) throw new Error("Failed to load devices");
      const data = await res.json();
      setDevices(data.devices || []);
    } catch (error) {
      console.error("Error loading devices:", error);
      toast({
        title: "Error",
        description: "Failed to load Tailscale devices",
        variant: "destructive",
      });
    } finally {
      setLoadingDevices(false);
    }
  };

  const loadSSHSetup = async () => {
    try {
      setLoadingSSH(true);
      const res = await fetch("/api/tailscale/ssh-setup");
      if (res.ok) {
        const data = await res.json();
        setSSHData({ sshPublicKey: data.sshPublicKey, command: data.command });
      }
    } catch (error) {
      console.error("Error loading SSH setup:", error);
    } finally {
      setLoadingSSH(false);
    }
  };

  const handleDisconnect = async () => {
    try {
      setDisconnecting(true);
      setShowDisconnectConfirm(false);

      const res = await fetch("/api/tailscale/disconnect", { method: "POST" });
      if (!res.ok) throw new Error("Failed to disconnect");

      localStorage.removeItem("isTailscaleConnected");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await providerPreferencesService.removeProvider("tailscale");
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", { detail: { providers: [] } })
        );
      } catch (prefErr) {
        console.warn("Failed to update provider preferences", prefErr);
      }

      toast({ title: "Disconnected", description: "Tailscale disconnected successfully" });
      router.push("/connectors");
    } catch (error) {
      console.error("Error disconnecting:", error);
      toast({
        title: "Error",
        description: "Failed to disconnect Tailscale",
        variant: "destructive",
      });
      setDisconnecting(false);
    }
  };

  const handleAuthorizeDevice = async (deviceId: string) => {
    try {
      setAuthorizingDevice(deviceId);
      const res = await fetch(`/api/tailscale/devices/${deviceId}/authorize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ authorized: true }),
      });
      if (!res.ok) throw new Error("Failed to authorize device");

      toast({ title: "Device Authorized", description: "Device has been authorized" });
      await loadDevices();
    } catch (error) {
      console.error("Error authorizing device:", error);
      toast({
        title: "Error",
        description: "Failed to authorize device",
        variant: "destructive",
      });
    } finally {
      setAuthorizingDevice(null);
    }
  };

  const confirmRemoveDevice = async () => {
    if (!deviceToRemove) return;

    try {
      setRemovingDevice(deviceToRemove.id);
      setDeviceToRemove(null);

      const res = await fetch(`/api/tailscale/devices/${deviceToRemove.id}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error("Failed to remove device");

      toast({
        title: "Device Removed",
        description: `${deviceToRemove.hostname} has been removed`,
      });
      await loadDevices();
    } catch (error) {
      console.error("Error removing device:", error);
      toast({
        title: "Error",
        description: "Failed to remove device",
        variant: "destructive",
      });
    } finally {
      setRemovingDevice(null);
    }
  };

  const handleCopy = async (text: string, type: "key" | "command") => {
    try {
      await copyToClipboard(text);
      setCopied(type);
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = setTimeout(() => setCopied(null), 2000);
    } catch (error) {
      console.error("Failed to copy:", error);
    }
  };

  return (
    <ConnectorAuthGuard connectorName="Tailscale">
      <div className="min-h-screen bg-black text-white p-8">
        <div className="max-w-6xl mx-auto">
          <div className="flex items-center gap-4 mb-8">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => router.push("/connectors")}
              className="text-zinc-400 hover:text-white"
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              Back to Connectors
            </Button>
          </div>

          <Card className="bg-zinc-950 border-zinc-800 mb-6">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-white text-2xl">Manage Tailscale</CardTitle>
                  <CardDescription className="text-zinc-400 mt-2">
                    {tailnetName
                      ? `Connected to ${tailnetName} \u2022 ${devices.length} device(s)`
                      : "View and manage your Tailscale network devices"}
                  </CardDescription>
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => { loadDevices(); loadSSHSetup(); }}
                    disabled={loadingDevices}
                    className="border-zinc-700 hover:bg-zinc-900"
                  >
                    <RefreshCw className={`h-4 w-4 mr-2 ${loadingDevices ? "animate-spin" : ""}`} />
                    Refresh
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setShowDisconnectConfirm(true)}
                    disabled={disconnecting}
                    className="text-red-400 hover:text-red-300 hover:bg-red-950/20"
                  >
                    {disconnecting ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <>
                        <LogOut className="h-4 w-4 mr-2" />
                        Disconnect
                      </>
                    )}
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {loadingDevices ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
                </div>
              ) : devices.length === 0 ? (
                <div className="text-center py-12">
                  <p className="text-zinc-400 mb-4">No devices found in your tailnet</p>
                </div>
              ) : (
                <div className="space-y-3">
                  {devices.map((device) => {
                    const online = isDeviceOnline(device.lastSeen);
                    return (
                      <div
                        key={device.id}
                        className="flex items-center justify-between p-4 bg-zinc-900 border border-zinc-800 rounded-lg"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-3 mb-2">
                            <h3 className="text-white font-medium truncate">
                              {device.hostname}
                            </h3>
                            <span
                              className={`text-xs px-2 py-1 rounded ${
                                online
                                  ? "bg-green-500/10 text-green-400"
                                  : "bg-red-500/10 text-red-400"
                              }`}
                            >
                              {online ? "Online" : "Offline"}
                            </span>
                            {!device.authorized && (
                              <span className="text-xs px-2 py-1 rounded bg-yellow-500/10 text-yellow-400">
                                Unauthorized
                              </span>
                            )}
                          </div>
                          <div className="grid grid-cols-1 md:grid-cols-4 gap-2 text-sm text-zinc-400">
                            <div>
                              <span className="text-zinc-500">IP:</span>{" "}
                              <code className="text-xs bg-zinc-950 px-1.5 py-0.5 rounded">
                                {device.addresses?.[0] || "\u2014"}
                              </code>
                            </div>
                            <div>
                              <span className="text-zinc-500">OS:</span>{" "}
                              {device.os || "\u2014"}
                            </div>
                            <div>
                              <span className="text-zinc-500">Last Seen:</span>{" "}
                              {formatTimeAgo(device.lastSeen)}
                            </div>
                            <div>
                              <span className="text-zinc-500">Version:</span>{" "}
                              {device.clientVersion || "\u2014"}
                            </div>
                          </div>
                          {device.tags && device.tags.length > 0 && (
                            <div className="flex gap-1.5 mt-2">
                              {device.tags.map((tag) => (
                                <span
                                  key={tag}
                                  className="text-xs px-2 py-0.5 rounded bg-zinc-800 text-zinc-300"
                                >
                                  {tag}
                                </span>
                              ))}
                            </div>
                          )}
                          <div className="mt-1 text-xs text-zinc-500 truncate">
                            {device.name}
                          </div>
                        </div>
                        <div className="flex gap-2 ml-4">
                          {!device.authorized && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleAuthorizeDevice(device.id)}
                              disabled={authorizingDevice === device.id}
                              className="border-green-700 text-green-400 hover:bg-green-950/20"
                            >
                              {authorizingDevice === device.id ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                              ) : (
                                <>
                                  <Shield className="h-4 w-4 mr-2" />
                                  Authorize
                                </>
                              )}
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setDeviceToRemove({ id: device.id, hostname: device.hostname })}
                            disabled={removingDevice === device.id}
                            className="text-red-400 hover:text-red-300 hover:bg-red-950/20"
                          >
                            {removingDevice === device.id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <>
                                <Trash2 className="h-4 w-4 mr-2" />
                                Remove
                              </>
                            )}
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="bg-zinc-950 border-zinc-800">
            <CardHeader>
              <CardTitle className="text-white text-lg flex items-center gap-2">
                <Key className="h-5 w-5" />
                SSH Setup
              </CardTitle>
              <CardDescription className="text-zinc-400">
                Add Aurora&apos;s SSH public key to your devices to enable remote command execution.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {loadingSSH ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-6 w-6 animate-spin text-zinc-400" />
                </div>
              ) : sshData ? (
                <div className="space-y-4">
                  <CopyableBlock
                    label="SSH Public Key"
                    content={sshData.sshPublicKey}
                    copied={copied === "key"}
                    onCopy={() => handleCopy(sshData.sshPublicKey, "key")}
                  />
                  <CopyableBlock
                    label="Quick Setup Command"
                    labelIcon={<Terminal className="h-4 w-4" />}
                    content={sshData.command}
                    copied={copied === "command"}
                    onCopy={() => handleCopy(sshData.command, "command")}
                    hint="Run this command on any device you want Aurora to access via SSH."
                  />
                </div>
              ) : (
                <p className="text-zinc-500 text-sm">SSH setup information unavailable.</p>
              )}
            </CardContent>
          </Card>

          <AlertDialog open={showDisconnectConfirm} onOpenChange={setShowDisconnectConfirm}>
            <AlertDialogContent className="bg-zinc-950 border-zinc-800">
              <AlertDialogHeader>
                <AlertDialogTitle className="text-white">
                  Disconnect Tailscale?
                </AlertDialogTitle>
                <AlertDialogDescription className="text-zinc-400">
                  This will disconnect your Tailscale account and remove stored credentials.
                  Aurora will no longer be able to access your tailnet devices.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel className="bg-zinc-900 border-zinc-800 hover:bg-zinc-800 text-white">
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleDisconnect}
                  className="bg-red-600 hover:bg-red-700 text-white"
                >
                  Disconnect
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <AlertDialog open={deviceToRemove !== null} onOpenChange={(open) => { if (!open) setDeviceToRemove(null); }}>
            <AlertDialogContent className="bg-zinc-950 border-zinc-800">
              <AlertDialogHeader>
                <AlertDialogTitle className="text-white">Remove Device?</AlertDialogTitle>
                <AlertDialogDescription className="text-zinc-400">
                  This will remove{" "}
                  <span className="font-semibold text-zinc-300">
                    {deviceToRemove?.hostname}
                  </span>{" "}
                  from your tailnet.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel className="bg-zinc-900 border-zinc-800 hover:bg-zinc-800 text-white">
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  onClick={confirmRemoveDevice}
                  className="bg-red-600 hover:bg-red-700 text-white"
                >
                  Remove Device
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>
    </ConnectorAuthGuard>
  );
}

function CopyableBlock({ label, labelIcon, content, copied, onCopy, hint }: {
  label: string;
  labelIcon?: React.ReactNode;
  content: string;
  copied: boolean;
  onCopy: () => void;
  hint?: string;
}) {
  return (
    <div>
      <p className="text-sm text-zinc-400 mb-2 flex items-center gap-2">
        {labelIcon}
        {label}
      </p>
      <div className="relative">
        <pre className="overflow-auto rounded-lg bg-zinc-900 border border-zinc-800 p-3 pr-12 text-xs font-mono text-zinc-100 whitespace-pre-wrap break-all">
          {content}
        </pre>
        <Button
          variant="ghost"
          size="sm"
          onClick={onCopy}
          className="absolute right-2 top-2 text-zinc-400 hover:text-zinc-100"
        >
          {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </Button>
      </div>
      {hint && <p className="text-xs text-zinc-500 mt-2">{hint}</p>}
    </div>
  );
}
