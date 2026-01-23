"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import {
  Server,
  Check,
  X,
  ChevronDown,
  ChevronUp,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
  Link as LinkIcon,
  BadgeCheck,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { useUserId } from "@/hooks/use-user-id";
import { isOvhEnabled } from "@/lib/feature-flags";

type VMSource = "auto" | "manual";
const CACHE_KEY = "vm-config-cache";
const CACHE_TTL_MS = 2 * 60 * 1000; // 2 minutes

type VM = {
  id: string;
  backendId?: number;
  name: string;
  platform: "OVH" | "Scaleway" | "Manual";
  ipAddress: string;
  sshKeyConfigured: boolean;
  connectionVerified?: boolean;
  region?: string;
  zone?: string;
  status?: string;
  imageName?: string;
  port?: number;
  sshJumpCommand?: string | null;
  sshKeyId?: number | null;
  source: VMSource;
  sshUsername?: string | null;
};

type ManagedKey = {
  id: number;
  provider: string;
  label?: string | null;
  publicKey?: string | null;
  createdAt?: string | null;
};

type ManualVMDraft = {
  name: string;
  ipAddress: string;
  port: string;
  sshJumpCommand: string;
  sshKeyId: string;
  sshUsername: string;
};

// Manual VM APIs go through Next.js API routes to avoid CORS issues
// OVH/Scaleway calls still use direct backend URL (CORS allowed for these endpoints)
const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL;

export default function VMConfig() {
  const { toast } = useToast();
  const { userId, isLoading: userLoading } = useUserId();

  const [vms, setVms] = useState<VM[]>([]);
  const [autoVms, setAutoVms] = useState<VM[]>([]);
  const [manualVms, setManualVms] = useState<VM[]>([]);
  const [sshKeys, setSshKeys] = useState<ManagedKey[]>([]);
  const [expandedVMs, setExpandedVMs] = useState<Set<string>>(new Set());
  const [selectedKeys, setSelectedKeys] = useState<Record<string, string>>({});
  const [sshUsernames, setSshUsernames] = useState<Record<string, string>>({});
  // vm.sshUsername = persisted value, manualDrafts[vm.id].sshUsername = editable draft, manualUsernames[vm.id] = test-only override.
  const [manualUsernames, setManualUsernames] = useState<
    Record<string, string>
  >({});
  const [manualDrafts, setManualDrafts] = useState<
    Record<string, ManualVMDraft>
  >({});
  const [manualForm, setManualForm] = useState<ManualVMDraft>({
    name: "",
    ipAddress: "",
    port: "22",
    sshJumpCommand: "",
    sshKeyId: "",
    sshUsername: "",
  });
  const [isLoading, setIsLoading] = useState(true);
  const [savingVMs, setSavingVMs] = useState<Set<string>>(new Set());
  const [savingManualId, setSavingManualId] = useState<string | null>(null);
  const [isManualOpen, setIsManualOpen] = useState(false);
  const hasInitialized = useRef(false);
  const cacheLoaded = useRef(false);

  const seedManualDrafts = (manualList: VM[]) => {
    const drafts: Record<string, ManualVMDraft> = {};
    manualList.forEach((vm) => {
      drafts[vm.id] = {
        name: vm.name,
        ipAddress: vm.ipAddress,
        port: String(vm.port ?? 22),
        sshJumpCommand: vm.sshJumpCommand || "",
        sshKeyId: vm.sshKeyId ? String(vm.sshKeyId) : "",
        sshUsername: vm.sshUsername || "",
      };
    });
    setManualDrafts(drafts);
  };

  const loadCache = () => {
    if (typeof window === "undefined") return null;
    try {
      const raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed?.timestamp || Date.now() - parsed.timestamp > CACHE_TTL_MS)
        return null;
      return parsed as {
        autoVms: VM[];
        manualVms: VM[];
        sshKeys: ManagedKey[];
      };
    } catch (err) {
      console.error("Failed to read VM config cache:", err);
      return null;
    }
  };

  const saveCache = (autoList: VM[], manualList: VM[], keys: ManagedKey[]) => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        CACHE_KEY,
        JSON.stringify({
          timestamp: Date.now(),
          autoVms: autoList,
          manualVms: manualList,
          sshKeys: keys,
        }),
      );
    } catch (err) {
      console.error("Failed to write VM config cache:", err);
    }
  };

  const getEffectiveUserId = async (resolved?: string): Promise<string> => {
    if (resolved || userId) return resolved || (userId as string);
    const res = await fetch("/api/getUserId");
    const data = await res.json();
    if (!data.userId) throw new Error("Not authenticated");
    return data.userId;
  };

  const refreshKeys = async () => {
    try {
      const res = await fetch(`/api/ssh-keys`, {
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to load SSH keys");
      const data = await res.json();
      const loaded = data.keys || [];
      setSshKeys(loaded);
      return loaded as ManagedKey[];
    } catch (err) {
      console.error("Failed to load SSH keys", err);
      return [];
    }
  };

  const fetchAutoVms = async (effectiveUserId: string) => {
    if (!backendUrl) return [];
    const all: VM[] = [];
    
    // OVH (only if feature flag is enabled)
    if (isOvhEnabled()) {
      try {
        const ovhResponse = await fetch(`${backendUrl}/ovh_api/ovh/instances`, {
          headers: { "X-User-ID": effectiveUserId },
          credentials: "include",
        });
        if (ovhResponse.ok) {
          const ovhData = await ovhResponse.json();
          const ovhVMs: VM[] = (ovhData.instances || []).map((instance: any) => ({
            id: `ovh-${instance.id}`,
            name: instance.name,
            platform: "OVH",
            ipAddress: instance.ipAddresses?.[0]?.ip || "N/A",
            sshKeyConfigured: !!instance.sshConfig,
            region: instance.region,
            status: instance.status,
            imageName: instance.imageName,
            source: "auto",
          }));
          all.push(...ovhVMs);
        }
      } catch (err) {
        console.error("Error fetching OVH VMs:", err);
      }
    }

    // Scaleway
    try {
      const scwResponse = await fetch(
        `${backendUrl}/scaleway_api/scaleway/instances`,
        {
          headers: { "X-User-ID": effectiveUserId },
          credentials: "include",
        },
      );
      if (scwResponse.ok) {
          const scwData = await scwResponse.json();
          const scwVMs: VM[] = (scwData.servers || []).map((server: any) => ({
            id: `scaleway-${server.id}`,
            name: server.name,
            platform: "Scaleway",
            ipAddress: server.public_ip?.address || "N/A",
            sshKeyConfigured: !!server.sshConfig,
            zone: server.zone,
            status: server.state,
            imageName: server.imageName,
            source: "auto",
          }));
          all.push(...scwVMs);
        }
      } catch (err) {
        console.error("Error fetching Scaleway VMs:", err);
      }

    return all;
  };

  const fetchManualVms = async () => {
    try {
      const res = await fetch(`/api/vms/manual`, {
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) {
        console.error("Failed to load manual VMs:", data.error || res.status);
        return [];
      }
      const manualList: VM[] = (data.vms || []).map((vm: any) => ({
        id: `manual-${vm.id}`,
        backendId: vm.id,
        name: vm.name,
        platform: "Manual",
        ipAddress: vm.ipAddress,
        port: vm.port ?? 22,
        sshJumpCommand: vm.sshJumpCommand,
        sshKeyId: vm.sshKeyId,
        sshKeyConfigured: vm.connectionVerified ?? false,
        sshUsername: vm.sshUsername,
        source: "manual",
        status: "Manual",
      }));
      // Seed drafts for edits
      seedManualDrafts(manualList);
      return manualList;
    } catch (err) {
      console.error("Error fetching manual VMs:", err);
      return [];
    }
  };

  const syncVms = (autoList: VM[], manualList: VM[]) => {
    setAutoVms(autoList);
    setManualVms(manualList);
    // Keep manual first so they're easy to spot
    setVms([...manualList, ...autoList]);
  };

  const init = async () => {
    const shouldShowSpinner =
      autoVms.length === 0 && manualVms.length === 0 && !cacheLoaded.current;
    if (shouldShowSpinner) {
      setIsLoading(true);
    }
    try {
      const effectiveUserId = await getEffectiveUserId();
      const [autoRes, manualRes, keysRes] = await Promise.allSettled([
        fetchAutoVms(effectiveUserId),
        fetchManualVms(),
        refreshKeys(),
      ]);

      const autoList = autoRes.status === "fulfilled" ? autoRes.value : [];
      const manualList =
        manualRes.status === "fulfilled" ? manualRes.value : [];
      const keysList = keysRes.status === "fulfilled" ? keysRes.value : sshKeys;
      syncVms(autoList, manualList);

      const canCache =
        autoRes.status === "fulfilled" &&
        manualRes.status === "fulfilled" &&
        keysRes.status === "fulfilled";
      if (keysRes.status === "rejected") {
        console.error("Failed to refresh keys:", keysRes.reason);
      }
      if (canCache) {
        saveCache(autoList, manualList, keysList || []);
      }
    } catch (err) {
      console.error("Error initializing VM config:", err);
      toast({
        title: "Error",
        description: "Failed to load VMs. Please try again.",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (userLoading) return;
    if (hasInitialized.current) return;
    const cached = loadCache();
    if (cached) {
      cacheLoaded.current = true;
      setAutoVms(cached.autoVms || []);
      setManualVms(cached.manualVms || []);
      setVms([...(cached.manualVms || []), ...(cached.autoVms || [])]);
      seedManualDrafts(cached.manualVms || []);
      setSshKeys(cached.sshKeys || []);
      setIsLoading(false);
    }
    hasInitialized.current = true;
    init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userLoading]);

  const toggleVM = (vmId: string) => {
    setExpandedVMs((prev) => {
      const next = new Set(prev);
      if (next.has(vmId)) next.delete(vmId);
      else next.add(vmId);
      return next;
    });
  };

  const setSavingState = (vmId: string, saving: boolean) => {
    setSavingVMs((prev) => {
      const next = new Set(prev);
      if (saving) next.add(vmId);
      else next.delete(vmId);
      return next;
    });
  };

  const getPlatformAndId = (
    vmId: string,
  ): { platform: "ovh" | "scaleway"; actualVmId: string } => {
    if (vmId.startsWith("ovh-")) {
      return { platform: "ovh", actualVmId: vmId.slice(4) };
    }
    if (vmId.startsWith("scaleway-")) {
      return { platform: "scaleway", actualVmId: vmId.slice(9) };
    }
    throw new Error("Invalid VM ID format");
  };

  // ---- Auto VM credential storage (existing flow) ----
  const handleSaveCredentials = async (vmId: string) => {
    const vm = vms.find((v) => v.id === vmId);
    const sshKeyId = selectedKeys[vmId];
    if (!vm || !sshKeyId) {
      toast({
        title: "Select a key",
        description: "Choose a managed SSH key before saving.",
        variant: "destructive",
      });
      return;
    }

    if ((!vm.imageName || vm.imageName === "Unknown") && !sshUsernames[vmId]) {
      toast({
        title: "Username Required",
        description:
          "Please specify the SSH username since the image could not be detected.",
        variant: "destructive",
      });
      return;
    }

    const isRunning = () => {
      const status = (vm.status || "").toLowerCase();
      if (vm.platform === "Scaleway") return status === "running";
      if (vm.platform === "OVH") return status === "active";
      return true;
    };

    if (!isRunning()) {
      toast({
        title: "VM is stopped",
        description: "Start the server before saving credentials.",
        variant: "destructive",
      });
      return;
    }

    setSavingState(vmId, true);

    try {
      toast({
        title: "Testing SSH Connection",
        description: `Validating credentials for ${vm.name}...`,
      });

      const effectiveUserId = await getEffectiveUserId();
      const { platform, actualVmId } = getPlatformAndId(vmId);

      const endpoint =
        platform === "ovh"
          ? `${backendUrl}/ovh_api/ovh/instances/${actualVmId}/ssh-keys`
          : `${backendUrl}/scaleway_api/scaleway/instances/${actualVmId}/ssh-keys`;

      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-User-ID": effectiveUserId,
        },
        body: JSON.stringify({
          sshKeyId: Number(sshKeyId),
          ...(sshUsernames[vmId] && { username: sshUsernames[vmId] }),
        }),
        credentials: "include",
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to save credentials");
      }

      setVms((prev) =>
        prev.map((item) =>
          item.id === vmId ? { ...item, sshKeyConfigured: true } : item,
        ),
      );

      setSshUsernames((names) => {
        const next = { ...names };
        delete next[vmId];
        return next;
      });
      setSelectedKeys((keys) => {
        const next = { ...keys };
        delete next[vmId];
        return next;
      });
      setExpandedVMs((prev) => {
        const next = new Set(prev);
        next.delete(vmId);
        return next;
      });

      toast({
        title: "SSH Access Configured",
        description: `Successfully validated and saved credentials for ${vm.name}`,
      });
    } catch (err) {
      console.error("Error saving credentials:", err);
      toast({
        title: "SSH Validation Failed",
        description:
          err instanceof Error ? err.message : "Failed to save SSH credentials",
        variant: "destructive",
      });
    } finally {
      setSavingState(vmId, false);
    }
  };

  const handleDeleteCredentials = async (vmId: string) => {
    const vm = vms.find((v) => v.id === vmId);
    if (!vm) return;

    setSavingState(vmId, true);

    try {
      const effectiveUserId = await getEffectiveUserId();
      const { platform, actualVmId } = getPlatformAndId(vmId);

      const endpoint =
        platform === "ovh"
          ? `${backendUrl}/ovh_api/ovh/instances/${actualVmId}/ssh-keys`
          : `${backendUrl}/scaleway_api/scaleway/instances/${actualVmId}/ssh-keys`;

      const response = await fetch(endpoint, {
        method: "DELETE",
        headers: {
          "X-User-ID": effectiveUserId,
        },
        credentials: "include",
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to delete credentials");
      }

      setVms((prev) =>
        prev.map((item) =>
          item.id === vmId ? { ...item, sshKeyConfigured: false } : item,
        ),
      );
      setExpandedVMs((prev) => new Set(prev).add(vmId));

      toast({
        title: "Credentials Removed",
        description: `SSH credentials for ${vm.name} have been deleted. You can now add new credentials.`,
      });
    } catch (err) {
      console.error("Error deleting credentials:", err);
      toast({
        title: "Failed to Delete",
        description:
          err instanceof Error
            ? err.message
            : "Failed to delete SSH credentials",
        variant: "destructive",
      });
    } finally {
      setSavingState(vmId, false);
    }
  };

  // ---- Manual VM actions ----
  const handleCreateManualVm = async () => {
    if (!manualForm.name || !manualForm.ipAddress) {
      toast({
        title: "Missing fields",
        description: "Name and IP are required for a manual VM.",
        variant: "destructive",
      });
      return;
    }
    setSavingManualId("new");
    try {
      const res = await fetch(`/api/vms/manual`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: manualForm.name,
          ipAddress: manualForm.ipAddress,
          port: Number(manualForm.port || 22),
          sshJumpCommand: manualForm.sshJumpCommand || null,
          sshKeyId: manualForm.sshKeyId ? Number(manualForm.sshKeyId) : null,
          sshUsername: manualForm.sshUsername || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to save manual VM");
      setManualForm({
        name: "",
        ipAddress: "",
        port: "22",
        sshJumpCommand: "",
        sshKeyId: "",
        sshUsername: "",
      });
      const manualList = await fetchManualVms();
      syncVms(autoVms, manualList);
      toast({
        title: "Manual VM saved",
        description: `${data.name || "VM"} saved successfully.`,
      });
    } catch (err) {
      console.error("Error creating manual VM:", err);
      toast({
        title: "Failed to save manual VM",
        description:
          err instanceof Error ? err.message : "Could not save manual VM.",
        variant: "destructive",
      });
    } finally {
      setSavingManualId(null);
    }
  };

  const handleUpdateManualVm = async (vm: VM) => {
    if (vm.source !== "manual" || !vm.backendId) return;
    const draft = manualDrafts[vm.id];
    if (!draft) return;
    setSavingManualId(vm.id);
    try {
      const res = await fetch(`/api/vms/manual/${vm.backendId}`, {
        method: "PUT",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: draft.name,
          ipAddress: draft.ipAddress,
          port: draft.port ? Number(draft.port) : null,
          sshJumpCommand: draft.sshJumpCommand || null,
          sshKeyId: draft.sshKeyId ? Number(draft.sshKeyId) : null,
          sshUsername: draft.sshUsername || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to update manual VM");
      const manualList = await fetchManualVms();
      syncVms(autoVms, manualList);
      toast({
        title: "Manual VM updated",
        description: `${draft.name} saved.`,
      });
    } catch (err) {
      console.error("Error updating manual VM:", err);
      toast({
        title: "Failed to update",
        description:
          err instanceof Error ? err.message : "Could not update manual VM.",
        variant: "destructive",
      });
    } finally {
      setSavingManualId(null);
    }
  };

  const handleDeleteManualVm = async (vm: VM) => {
    if (vm.source !== "manual" || !vm.backendId) return;
    setSavingManualId(vm.id);
    try {
      const res = await fetch(`/api/vms/manual/${vm.backendId}`, {
        method: "DELETE",
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to delete manual VM");
      const manualList = await fetchManualVms();
      syncVms(autoVms, manualList);
      setExpandedVMs((prev) => {
        const next = new Set(prev);
        next.delete(vm.id);
        return next;
      });
      toast({ title: "Manual VM deleted", description: `${vm.name} removed.` });
    } catch (err) {
      console.error("Error deleting manual VM:", err);
      toast({
        title: "Delete failed",
        description:
          err instanceof Error ? err.message : "Could not delete manual VM.",
        variant: "destructive",
      });
    } finally {
      setSavingManualId(null);
    }
  };

  const handleTestManualConnection = async (vm: VM) => {
    const username =
      manualUsernames[vm.id] ||
      manualDrafts[vm.id]?.sshUsername ||
      vm.sshUsername;
    if (!username) {
      toast({
        title: "Username required",
        description: "Provide an SSH username to test the connection.",
        variant: "destructive",
      });
      return;
    }
    setSavingManualId(vm.id);
    try {
      const payload: any = {
        username,
        sshKeyId:
          vm.sshKeyId ||
          (manualDrafts[vm.id]?.sshKeyId
            ? Number(manualDrafts[vm.id].sshKeyId)
            : undefined),
        sshJumpCommand:
          manualDrafts[vm.id]?.sshJumpCommand || vm.sshJumpCommand || null,
        sshUsername: manualDrafts[vm.id]?.sshUsername || vm.sshUsername,
      };
      // If we have a stored VM, send vmId to reuse saved fields; else include full data
      if (vm.backendId) {
        payload.vmId = vm.backendId;
      } else {
        payload.ipAddress = vm.ipAddress;
        payload.port = vm.port || 22;
      }
      const res = await fetch(`/api/vms/check-connection`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.success === false)
        throw new Error(data.error || "SSH connection failed");
      toast({
        title: "Connection successful",
        description: data.connectedAs
          ? `Connected as ${data.connectedAs}`
          : "SSH check passed.",
      });
    } catch (err) {
      console.error("Manual SSH check failed:", err);
      toast({
        title: "Connection failed",
        description:
          err instanceof Error ? err.message : "SSH validation failed.",
        variant: "destructive",
      });
    } finally {
      setSavingManualId(null);
    }
  };

  const totalCount = vms.length;
  const ovhCount = vms.filter((vm) => vm.platform === "OVH").length;
  const scalewayCount = vms.filter((vm) => vm.platform === "Scaleway").length;
  const manualCount = manualVms.length;

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="bg-background">
        <div className="mx-auto max-w-7xl px-6 pb-6 pt-10">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h1 className="text-balance text-2xl font-semibold tracking-tight">
                VM SSH Access
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Configure credentials for virtual machines
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-7xl px-6 py-8">
        <Collapsible open={isManualOpen} onOpenChange={setIsManualOpen}>
          <div className="space-y-8">
            {/* VM Table/List */}
            <div className="overflow-hidden rounded-lg border bg-card shadow-sm">
              <div className="grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)_minmax(0,2fr)_minmax(0,3fr)_minmax(0,2fr)_minmax(0,2fr)] gap-4 border-b bg-muted/50 px-6 py-3 text-sm font-medium text-muted-foreground">
                <div>VM Name</div>
                <div>Platform</div>
                <div>Source</div>
                <div>IP / Port</div>
                <div>Status</div>
                <div className="flex items-center justify-end gap-2">
                  <span className="text-xs text-muted-foreground">
                    Manual VM
                  </span>
                  <CollapsibleTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 rounded-full"
                      aria-label={
                        isManualOpen ? "Hide manual VM form" : "Add manual VM"
                      }
                    >
                      <Plus
                        className={`h-4 w-4 transition-transform ${isManualOpen ? "rotate-45" : ""}`}
                      />
                    </Button>
                  </CollapsibleTrigger>
                </div>
              </div>

              <CollapsibleContent className="overflow-hidden transition-all data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down data-[state=open]:border-b data-[state=open]:bg-muted/10">
                <div className="p-6">
                  {/* Manual VM creation */}
                  <div className="mb-4">
                    <h2 className="text-lg font-semibold">Add Manual VM</h2>
                    <p className="text-sm text-muted-foreground">
                      Store bastion/VM connection details with a managed SSH
                      key.
                    </p>
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor="vm-name">Name</Label>
                      <Input
                        id="vm-name"
                        placeholder="prod-api"
                        value={manualForm.name}
                        onChange={(e) =>
                          setManualForm((p) => ({ ...p, name: e.target.value }))
                        }
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="vm-ip">IP / Host</Label>
                      <Input
                        id="vm-ip"
                        placeholder="10.0.0.5"
                        value={manualForm.ipAddress}
                        onChange={(e) =>
                          setManualForm((p) => ({
                            ...p,
                            ipAddress: e.target.value,
                          }))
                        }
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="vm-port">Port</Label>
                      <Input
                        id="vm-port"
                        type="number"
                        min={1}
                        max={65535}
                        value={manualForm.port}
                        onChange={(e) =>
                          setManualForm((p) => ({ ...p, port: e.target.value }))
                        }
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="vm-key">SSH Key</Label>
                      <Select
                        value={manualForm.sshKeyId}
                        onValueChange={(val) =>
                          setManualForm((p) => ({ ...p, sshKeyId: val }))
                        }
                      >
                        <SelectTrigger id="vm-key">
                          <SelectValue placeholder="Select managed key" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            <SelectLabel>Managed keys</SelectLabel>
                            {sshKeys.map((key) => (
                              <SelectItem key={key.id} value={String(key.id)}>
                                {key.label || key.provider}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Generate keys in Settings → SSH Keys.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="vm-username">SSH Username</Label>
                      <Input
                        id="vm-username"
                        placeholder="ubuntu, debian, root..."
                        value={manualForm.sshUsername}
                        onChange={(e) =>
                          setManualForm((p) => ({
                            ...p,
                            sshUsername: e.target.value,
                          }))
                        }
                      />
                    </div>
                    <div className="md:col-span-2 space-y-2">
                      <Label htmlFor="vm-jump">
                        SSH Jump Command (optional)
                      </Label>
                      <Textarea
                        id="vm-jump"
                        placeholder="ssh -J bastion -p 2200 user@target"
                        value={manualForm.sshJumpCommand}
                        onChange={(e) =>
                          setManualForm((p) => ({
                            ...p,
                            sshJumpCommand: e.target.value,
                          }))
                        }
                      />
                      <p className="text-xs text-muted-foreground">
                        Paste the exact jump command you would run locally.
                        Supports -J/ProxyJump and -p target ports.
                      </p>
                    </div>
                  </div>
                  <div className="mt-4 flex items-center gap-3">
                    <div className="ml-auto flex gap-2">
                      <Button
                        variant="outline"
                        onClick={() =>
                          setManualForm({
                            name: "",
                            ipAddress: "",
                            port: "22",
                            sshJumpCommand: "",
                            sshKeyId: "",
                            sshUsername: "",
                          })
                        }
                      >
                        Clear
                      </Button>
                      <Button
                        onClick={handleCreateManualVm}
                        disabled={savingManualId === "new"}
                      >
                        {savingManualId === "new" ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Saving...
                          </>
                        ) : (
                          <>
                            <Plus className="mr-2 h-4 w-4" />
                            Save Manual VM
                          </>
                        )}
                      </Button>
                    </div>
                  </div>
                </div>
              </CollapsibleContent>

              {vms.length === 0 ? (
                <div className="px-6 py-12 text-center text-muted-foreground">
                  No VMs found. Create or import VMs to configure them here.
                </div>
              ) : (
                vms.map((vm, index) => (
                  <div
                    key={vm.id}
                    className={index !== vms.length - 1 ? "border-b" : ""}
                  >
                    <button
                      onClick={() => toggleVM(vm.id)}
                      className="grid w-full grid-cols-[minmax(0,3fr)_minmax(0,2fr)_minmax(0,2fr)_minmax(0,3fr)_minmax(0,2fr)_minmax(0,2fr)] gap-4 px-6 py-4 text-left transition-colors hover:bg-muted/30"
                    >
                      <div className="flex items-center gap-3">
                        <Server className="h-4 w-4 text-muted-foreground" />
                        <span className="font-medium">{vm.name}</span>
                      </div>
                      <div className="flex items-center">
                        <div className="flex items-center gap-2">
                          {vm.platform === "OVH" && (
                            <Image
                              src="/ovh.svg"
                              alt="OVH"
                              width={20}
                              height={20}
                              className="flex-shrink-0"
                            />
                          )}
                          {vm.platform === "Scaleway" && (
                            <Image
                              src="/scaleway.svg"
                              alt="Scaleway"
                              width={20}
                              height={20}
                              className="flex-shrink-0"
                            />
                          )}
                          {vm.platform === "Manual" && (
                            <LinkIcon className="h-4 w-4 text-muted-foreground" />
                          )}
                          <span className="text-sm text-muted-foreground">
                            {vm.platform}
                          </span>
                        </div>
                      </div>
                      <div className="flex items-center">
                        <Badge variant="outline" className="text-xs">
                          {vm.source === "manual"
                            ? "Manual"
                            : "Auto-discovered"}
                        </Badge>
                      </div>
                      <div className="flex items-center font-mono text-sm text-muted-foreground">
                        {vm.ipAddress}
                        {vm.port ? `:${vm.port}` : ""}
                      </div>
                      <div className="flex items-center justify-start">
                        {vm.sshKeyConfigured ? (
                          <div className="flex items-center gap-1.5 text-green-600 dark:text-green-500">
                            <Check className="h-4 w-4" />
                            <span className="text-sm font-medium">Ready</span>
                          </div>
                        ) : (
                          <div className="flex items-center gap-1.5 text-muted-foreground">
                            <X className="h-4 w-4" />
                            <span className="text-sm">Not configured</span>
                          </div>
                        )}
                        {expandedVMs.has(vm.id) ? (
                          <ChevronUp className="ml-2 h-4 w-4 text-muted-foreground" />
                        ) : (
                          <ChevronDown className="ml-2 h-4 w-4 text-muted-foreground" />
                        )}
                      </div>
                      <div />
                    </button>

                    {expandedVMs.has(vm.id) && (
                      <div className="border-t bg-muted/20 px-6 py-6">
                        <div className="mx-auto max-w-3xl space-y-4">
                          {vm.source === "manual" ? (
                            <>
                              <div className="grid gap-4 md:grid-cols-2">
                                <div className="space-y-2">
                                  <Label>Name</Label>
                                  <Input
                                    value={manualDrafts[vm.id]?.name || ""}
                                    onChange={(e) =>
                                      setManualDrafts((drafts) => ({
                                        ...drafts,
                                        [vm.id]: {
                                          ...(drafts[vm.id] || manualForm),
                                          name: e.target.value,
                                        },
                                      }))
                                    }
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label>IP / Host</Label>
                                  <Input
                                    value={manualDrafts[vm.id]?.ipAddress || ""}
                                    onChange={(e) =>
                                      setManualDrafts((drafts) => ({
                                        ...drafts,
                                        [vm.id]: {
                                          ...(drafts[vm.id] || manualForm),
                                          ipAddress: e.target.value,
                                        },
                                      }))
                                    }
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label>Port</Label>
                                  <Input
                                    type="number"
                                    min={1}
                                    max={65535}
                                    value={manualDrafts[vm.id]?.port || "22"}
                                    onChange={(e) =>
                                      setManualDrafts((drafts) => ({
                                        ...drafts,
                                        [vm.id]: {
                                          ...(drafts[vm.id] || manualForm),
                                          port: e.target.value,
                                        },
                                      }))
                                    }
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label>SSH Key</Label>
                                  <Select
                                    value={manualDrafts[vm.id]?.sshKeyId || ""}
                                    onValueChange={(val) =>
                                      setManualDrafts((drafts) => ({
                                        ...drafts,
                                        [vm.id]: {
                                          ...(drafts[vm.id] || manualForm),
                                          sshKeyId: val,
                                        },
                                      }))
                                    }
                                  >
                                    <SelectTrigger>
                                      <SelectValue placeholder="Select managed key" />
                                    </SelectTrigger>
                                    <SelectContent>
                                      <SelectGroup>
                                        <SelectLabel>Managed keys</SelectLabel>
                                        {sshKeys.map((key) => (
                                          <SelectItem
                                            key={key.id}
                                            value={String(key.id)}
                                          >
                                            {key.label || key.provider}
                                          </SelectItem>
                                        ))}
                                      </SelectGroup>
                                    </SelectContent>
                                  </Select>
                                </div>
                                <div className="space-y-2">
                                  <Label>SSH Username</Label>
                                  <Input
                                    placeholder="ubuntu, debian, root..."
                                    value={
                                      manualDrafts[vm.id]?.sshUsername || ""
                                    }
                                    onChange={(e) =>
                                      setManualDrafts((drafts) => ({
                                        ...drafts,
                                        [vm.id]: {
                                          ...(drafts[vm.id] || manualForm),
                                          sshUsername: e.target.value,
                                        },
                                      }))
                                    }
                                  />
                                </div>
                              </div>
                              <div className="space-y-2">
                                <Label>SSH Jump Command (optional)</Label>
                                <Textarea
                                  placeholder="ssh -J bastion -p 2200 user@target"
                                  value={
                                    manualDrafts[vm.id]?.sshJumpCommand || ""
                                  }
                                  onChange={(e) =>
                                    setManualDrafts((drafts) => ({
                                      ...drafts,
                                      [vm.id]: {
                                        ...(drafts[vm.id] || manualForm),
                                        sshJumpCommand: e.target.value,
                                      },
                                    }))
                                  }
                                  className="font-mono text-xs"
                                />
                                <p className="text-xs text-muted-foreground">
                                  Supports -J/ProxyJump and -p target ports.
                                  Public key must be on bastion/target.
                                </p>
                              </div>
                              <div className="flex flex-col gap-3 rounded-md border border-border/60 bg-background p-4">
                                <div className="flex items-center gap-2">
                                  <ShieldCheck className="h-4 w-4 text-muted-foreground" />
                                  <p className="text-sm text-muted-foreground">
                                    Test connection
                                  </p>
                                </div>
                                <div className="flex flex-col gap-2 md:flex-row md:items-center">
                                  <Input
                                    placeholder="SSH username"
                                    className="md:max-w-xs"
                                    value={
                                      manualUsernames[vm.id] ??
                                      manualDrafts[vm.id]?.sshUsername ??
                                      ""
                                    }
                                    onChange={(e) =>
                                      setManualUsernames((prev) => ({
                                        ...prev,
                                        [vm.id]: e.target.value,
                                      }))
                                    }
                                  />
                                  <div className="flex gap-2 md:ml-auto">
                                    <Button
                                      variant="outline"
                                      onClick={() => handleUpdateManualVm(vm)}
                                      disabled={savingManualId === vm.id}
                                    >
                                      {savingManualId === vm.id ? (
                                        <>
                                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                          Saving...
                                        </>
                                      ) : (
                                        "Save Changes"
                                      )}
                                    </Button>
                                    <Button
                                      onClick={() =>
                                        handleTestManualConnection(vm)
                                      }
                                      disabled={savingManualId === vm.id}
                                    >
                                      {savingManualId === vm.id ? (
                                        <>
                                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                          Testing...
                                        </>
                                      ) : (
                                        <>
                                          <BadgeCheck className="mr-2 h-4 w-4" />
                                          Test Connection
                                        </>
                                      )}
                                    </Button>
                                    <Button
                                      variant="outline"
                                      className="border-red-200 text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950/20"
                                      onClick={() => handleDeleteManualVm(vm)}
                                      disabled={savingManualId === vm.id}
                                    >
                                      <X className="mr-2 h-4 w-4" />
                                      Delete
                                    </Button>
                                  </div>
                                </div>
                              </div>
                            </>
                          ) : (
                            <>
                              <div className="space-y-2">
                                <div className="flex items-center justify-between mb-2">
                                  <div>
                                    <p className="text-sm text-muted-foreground">
                                      <span className="font-medium">
                                        IP Address:
                                      </span>{" "}
                                      {vm.ipAddress}
                                    </p>
                                    {vm.imageName && (
                                      <p className="text-xs text-muted-foreground mt-1">
                                        Image: {vm.imageName}
                                      </p>
                                    )}
                                  </div>
                                </div>
                              </div>
                              {(!vm.imageName ||
                                vm.imageName === "Unknown") && (
                                <div className="space-y-2">
                                  <label className="text-sm font-medium text-orange-600 dark:text-orange-400">
                                    SSH Username (required - image unknown)
                                  </label>
                                  <input
                                    type="text"
                                    placeholder={
                                      vm.platform === "Scaleway"
                                        ? "root"
                                        : "debian, ubuntu, root, etc."
                                    }
                                    className="w-full rounded-md border border-orange-300 bg-background px-3 py-2 text-sm focus:border-orange-500 focus:ring-orange-500"
                                    value={sshUsernames[vm.id] || ""}
                                    onChange={(e) => {
                                      setSshUsernames((usernames) => ({
                                        ...usernames,
                                        [vm.id]: e.target.value,
                                      }));
                                    }}
                                    disabled={
                                      vm.sshKeyConfigured ||
                                      savingVMs.has(vm.id)
                                    }
                                  />
                                  <p className="text-xs text-orange-600 dark:text-orange-400">
                                    Image name could not be detected. Please
                                    specify the SSH username for this VM.
                                  </p>
                                </div>
                              )}
                              <div className="space-y-2">
                                <Label className="text-sm font-medium">
                                  Managed SSH Key
                                </Label>
                                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                                  <Select
                                    value={selectedKeys[vm.id] || ""}
                                    onValueChange={(val) =>
                                      setSelectedKeys((prev) => ({
                                        ...prev,
                                        [vm.id]: val,
                                      }))
                                    }
                                    disabled={
                                      vm.sshKeyConfigured ||
                                      savingVMs.has(vm.id)
                                    }
                                  >
                                    <SelectTrigger className="sm:min-w-[240px]">
                                      <SelectValue
                                        placeholder={
                                          sshKeys.length
                                            ? "Select managed key"
                                            : "No managed keys"
                                        }
                                      />
                                    </SelectTrigger>
                                    <SelectContent>
                                      <SelectGroup>
                                        <SelectLabel>Managed keys</SelectLabel>
                                        {sshKeys.map((key) => (
                                          <SelectItem
                                            key={key.id}
                                            value={String(key.id)}
                                          >
                                            {key.label || key.provider}
                                          </SelectItem>
                                        ))}
                                      </SelectGroup>
                                    </SelectContent>
                                  </Select>
                                  <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    onClick={refreshKeys}
                                    disabled={savingVMs.has(vm.id)}
                                  >
                                    <RefreshCw className="mr-2 h-4 w-4" />
                                    Refresh keys
                                  </Button>
                                </div>
                                <p className="text-xs text-muted-foreground">
                                  Generate keys in Settings → SSH Keys, then
                                  select one to grant access.
                                </p>
                              </div>

                              {vm.sshKeyConfigured ? (
                                <div className="flex items-center justify-between rounded-lg border border-green-200 bg-green-50 dark:border-green-900 dark:bg-green-950/20 px-4 py-3">
                                  <div className="flex items-center gap-2">
                                    <Check className="h-4 w-4 text-green-600 dark:text-green-500" />
                                    <span className="text-sm font-medium text-green-900 dark:text-green-100">
                                      SSH credentials configured and validated
                                    </span>
                                  </div>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() =>
                                      handleDeleteCredentials(vm.id)
                                    }
                                    disabled={savingVMs.has(vm.id)}
                                    className="border-red-200 text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950/20"
                                  >
                                    {savingVMs.has(vm.id) ? (
                                      <>
                                        <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                                        Removing...
                                      </>
                                    ) : (
                                      <>
                                        <X className="mr-2 h-3 w-3" />
                                        Remove & Retry
                                      </>
                                    )}
                                  </Button>
                                </div>
                              ) : (
                                <div className="flex justify-end gap-2">
                                  <Button
                                    variant="outline"
                                    onClick={() => toggleVM(vm.id)}
                                  >
                                    Cancel
                                  </Button>
                                  <Button
                                    onClick={() => handleSaveCredentials(vm.id)}
                                    disabled={
                                      !selectedKeys[vm.id] ||
                                      savingVMs.has(vm.id)
                                    }
                                  >
                                    {savingVMs.has(vm.id) ? (
                                      <>
                                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                        Testing SSH...
                                      </>
                                    ) : (
                                      "Save Credentials"
                                    )}
                                  </Button>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        </Collapsible>
      </div>
    </div>
  );
}
