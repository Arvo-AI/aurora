"use client";

import { useEffect, useState } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { useRouter } from "next/navigation";
import { Pencil, Check, X } from "lucide-react";
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
    if (isLoaded && !user) router.replace("/sign-in");
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
        setOrg((prev) => (prev ? { ...prev, name: data.name } : prev));
        toast({ title: "Name updated" });
      }
    } catch {
      toast({ title: "Failed to update", variant: "destructive" });
    } finally {
      setSavingName(false);
      setEditingName(false);
    }
  }

  if (!isLoaded || loading) {
    return <div className="flex items-center justify-center h-full text-muted-foreground text-sm">Loading...</div>;
  }

  if (!org) {
    return <div className="flex items-center justify-center h-full text-muted-foreground text-sm">No organization found.</div>;
  }

  const initial = org.name.charAt(0).toUpperCase();

  return (
    <div className="max-w-5xl mx-auto px-6 py-10">
      {/* Minimal header — just the name, editable */}
      <div className="mb-10">
        <div className="flex items-center gap-4 mb-1">
          <div className="h-10 w-10 rounded-lg bg-foreground text-background flex items-center justify-center text-lg font-semibold select-none">
            {initial}
          </div>
          {editingName ? (
            <div className="flex items-center gap-2">
              <Input
                value={nameInput}
                onChange={(e) => setNameInput(e.target.value)}
                className="text-xl font-semibold h-9 max-w-xs border-none shadow-none focus-visible:ring-1 px-2"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveName();
                  if (e.key === "Escape") { setEditingName(false); setNameInput(org.name); }
                }}
              />
              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={saveName} disabled={savingName}>
                <Check className="h-3.5 w-3.5" />
              </Button>
              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => { setEditingName(false); setNameInput(org.name); }}>
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          ) : (
            <button
              onClick={isAdmin ? () => setEditingName(true) : undefined}
              className={`text-xl font-semibold tracking-tight ${isAdmin ? "hover:text-muted-foreground transition-colors cursor-text" : "cursor-default"}`}
            >
              {org.name}
            </button>
          )}
        </div>
        <p className="text-[13px] text-muted-foreground ml-14">
          <span className="font-mono text-xs text-muted-foreground/60">{org.slug}</span>
          <span className="mx-2 text-border">·</span>
          {org.members.length} member{org.members.length !== 1 ? "s" : ""}
          <span className="mx-2 text-border">·</span>
          since{" "}
          {org.createdAt
            ? new Date(org.createdAt).toLocaleDateString(undefined, { month: "short", year: "numeric" })
            : "recently"}
        </p>
      </div>

      {/* Underline-style tabs — no pill background */}
      <Tabs defaultValue="overview" className="w-full">
        <div className="border-b border-border mb-8">
          <TabsList className="h-auto p-0 bg-transparent rounded-none gap-6">
            {["overview", "members", "integrations", "preferences", "activity"].map((tab) => (
              <TabsTrigger
                key={tab}
                value={tab}
                className="px-0 pb-2.5 pt-0 rounded-none border-b-2 border-transparent data-[state=active]:border-foreground data-[state=active]:bg-transparent data-[state=active]:shadow-none text-muted-foreground data-[state=active]:text-foreground capitalize text-sm font-medium"
              >
                {tab}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="overview">
          <OrgOverview org={org} isAdmin={isAdmin} />
        </TabsContent>
        <TabsContent value="members">
          <OrgMembers org={org} currentUserId={user?.id || ""} isAdmin={isAdmin} onMembersChanged={fetchOrg} />
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
