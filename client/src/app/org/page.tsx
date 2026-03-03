"use client";

import { useEffect, useState } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { useRouter } from "next/navigation";
import { Building2, Pencil, Check, X } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "@/hooks/use-toast";
import OrgOverview from "./components/OrgOverview";
import OrgMembers from "./components/OrgMembers";
import OrgIntegrations from "./components/OrgIntegrations";
import OrgPreferences from "./components/OrgPreferences";
import OrgActivity from "./components/OrgActivity";

interface OrgData {
  id: string;
  name: string;
  slug: string;
  createdBy: string;
  createdAt: string;
  members: OrgMember[];
}

export interface OrgMember {
  id: string;
  email: string;
  name: string | null;
  role: string;
  createdAt: string | null;
}

export default function OrgPage() {
  const { user, isLoaded } = useUser();
  const router = useRouter();
  const [org, setOrg] = useState<OrgData | null>(null);
  const [loading, setLoading] = useState(true);
  const [editingName, setEditingName] = useState(false);
  const [nameInput, setNameInput] = useState("");
  const [savingName, setSavingName] = useState(false);

  const isAdmin = user?.role === "admin";

  useEffect(() => {
    if (isLoaded && !user) {
      router.replace("/sign-in");
    }
  }, [isLoaded, user, router]);

  useEffect(() => {
    if (user) fetchOrg();
  }, [user]);

  async function fetchOrg() {
    try {
      const res = await fetch("/api/orgs/current");
      if (res.ok) {
        const data = await res.json();
        setOrg(data);
        setNameInput(data.name);
      }
    } catch (err) {
      console.error("Failed to fetch org:", err);
    } finally {
      setLoading(false);
    }
  }

  async function saveName() {
    if (!nameInput.trim() || nameInput === org?.name) {
      setEditingName(false);
      return;
    }
    setSavingName(true);
    try {
      const res = await fetch("/api/orgs/current", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nameInput.trim() }),
      });
      if (res.ok) {
        const data = await res.json();
        setOrg((prev) => prev ? { ...prev, name: data.name } : prev);
        toast({ title: "Organization updated", description: `Name changed to "${data.name}"` });
      }
    } catch {
      toast({ title: "Failed to update", variant: "destructive" });
    } finally {
      setSavingName(false);
      setEditingName(false);
    }
  }

  if (!isLoaded || loading) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!org) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        No organization found.
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto px-6 py-8">
      {/* Header with inline-editable org name */}
      <div className="flex items-start gap-4 mb-8">
        <div className="h-12 w-12 rounded-xl bg-primary/10 flex items-center justify-center flex-shrink-0">
          <Building2 className="h-6 w-6 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          {editingName ? (
            <div className="flex items-center gap-2">
              <Input
                value={nameInput}
                onChange={(e) => setNameInput(e.target.value)}
                className="text-2xl font-bold h-10 max-w-sm"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveName();
                  if (e.key === "Escape") { setEditingName(false); setNameInput(org.name); }
                }}
              />
              <Button size="icon" variant="ghost" className="h-8 w-8" onClick={saveName} disabled={savingName}>
                <Check className="h-4 w-4" />
              </Button>
              <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => { setEditingName(false); setNameInput(org.name); }}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2 group">
              <h1 className="text-2xl font-bold truncate">{org.name}</h1>
              {isAdmin && (
                <button
                  onClick={() => setEditingName(true)}
                  className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded hover:bg-muted"
                >
                  <Pencil className="h-4 w-4 text-muted-foreground" />
                </button>
              )}
            </div>
          )}
          <p className="text-sm text-muted-foreground mt-0.5">
            {org.members.length} member{org.members.length !== 1 ? "s" : ""} · Created{" "}
            {org.createdAt
              ? new Date(org.createdAt).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })
              : "recently"}
          </p>
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="overview" className="w-full">
        <TabsList className="w-full justify-start h-11 bg-muted/50 p-1 rounded-lg mb-6">
          <TabsTrigger value="overview" className="px-4">Overview</TabsTrigger>
          <TabsTrigger value="members" className="px-4">Members</TabsTrigger>
          <TabsTrigger value="integrations" className="px-4">Integrations</TabsTrigger>
          <TabsTrigger value="preferences" className="px-4">Preferences</TabsTrigger>
          <TabsTrigger value="activity" className="px-4">Activity</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <OrgOverview org={org} isAdmin={isAdmin} />
        </TabsContent>

        <TabsContent value="members">
          <OrgMembers
            org={org}
            currentUserId={user?.id || ""}
            isAdmin={isAdmin}
            onMembersChanged={fetchOrg}
          />
        </TabsContent>

        <TabsContent value="integrations">
          <OrgIntegrations />
        </TabsContent>

        <TabsContent value="preferences">
          <OrgPreferences isAdmin={isAdmin} />
        </TabsContent>

        <TabsContent value="activity">
          <OrgActivity />
        </TabsContent>
      </Tabs>
    </div>
  );
}
