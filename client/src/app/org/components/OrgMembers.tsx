"use client";

import { useState, useCallback, useEffect, Fragment } from "react";
import { Check, Minus, Plus, Loader2, ChevronDown, UserMinus, Users, Mail, Clock, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/hooks/use-toast";
import { VALID_ROLES, ROLE_META, type UserRole } from "@/lib/roles";
import type { OrgMember } from "@/components/OrgSettings";

const PERMISSION_TABLE: {
  category: string;
  features: { name: string; viewer: boolean; editor: boolean; admin: boolean }[];
}[] = [
  {
    category: "Incidents",
    features: [
      { name: "View incidents & alerts", viewer: true, editor: true, admin: true },
      { name: "Update & resolve incidents", viewer: false, editor: true, admin: true },
      { name: "Apply suggestions & merge", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Postmortems",
    features: [
      { name: "View postmortems", viewer: true, editor: true, admin: true },
      { name: "Edit & export postmortems", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Chat & Knowledge Base",
    features: [
      { name: "Use the chat assistant", viewer: true, editor: true, admin: true },
      { name: "Upload & manage documents", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Integrations",
    features: [
      { name: "View connector status", viewer: true, editor: true, admin: true },
      { name: "Connect & disconnect", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Administration",
    features: [
      { name: "Configure LLM providers", viewer: false, editor: false, admin: true },
      { name: "Manage users & roles", viewer: false, editor: false, admin: true },
      { name: "Organization settings", viewer: false, editor: false, admin: true },
    ],
  },
];

function AddUserDialog({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [role, setRole] = useState<string>("viewer");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [step, setStep] = useState<"email" | "new-user">("email");
  const [checking, setChecking] = useState(false);

  function reset() {
    setEmail(""); setName(""); setPassword(""); setConfirmPassword(""); setRole("viewer"); setError(""); setStep("email"); setChecking(false);
  }

  async function handleEmailSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!email) { setError("Email is required"); return; }

    setChecking(true);
    try {
      const checkRes = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, role, check_only: true }),
      });
      const checkData = await checkRes.json();

      if (!checkRes.ok) { setError(checkData.error || "Something went wrong"); return; }

      if (checkData.exists) {
        const res = await fetch("/api/admin/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, role }),
        });
        const data = await res.json();

        if (!res.ok) { setError(data.error || "Something went wrong"); return; }

        if (data.invited) {
          toast({
            title: "Invitation sent",
            description: `${data.name || data.email} already has an account. They can accept the invitation from the Invitations tab in their Organization settings.`,
            duration: 8000,
          });
          reset();
          setOpen(false);
          onCreated();
          return;
        }
      } else {
        setStep("new-user");
      }
    } catch {
      setError("Something went wrong");
    } finally {
      setChecking(false);
    }
  }

  async function handleCreateUser(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!password) { setError("Password is required"); return; }
    if (password.length < 8) { setError("Password must be at least 8 characters"); return; }
    if (password !== confirmPassword) { setError("Passwords do not match"); return; }

    setSaving(true);
    try {
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, name, role }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Failed to create user"); return; }

      toast({ title: "Member added", description: `${data.name || data.email || email} joined as ${role}` });
      reset();
      setOpen(false);
      onCreated();
    } catch {
      setError("Something went wrong");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) reset(); }}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5 h-8">
          <Plus className="h-3.5 w-3.5" />
          Add
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        {step === "email" ? (
          <form onSubmit={handleEmailSubmit}>
            <DialogHeader>
              <DialogTitle>Add team member</DialogTitle>
              <DialogDescription>
                Enter their email address. If they already have an account, they&apos;ll receive an invitation to join your organization.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-1.5">
                <Label htmlFor="add-email" className="text-xs">Email</Label>
                <Input id="add-email" type="email" placeholder="jane@company.com" required value={email} onChange={(e) => setEmail(e.target.value)} className="h-9" autoFocus />
              </div>
              <div className="grid gap-1.5">
                <Label className="text-xs">Role</Label>
                <Select value={role} onValueChange={setRole}>
                  <SelectTrigger className="h-9 w-full [&>span]:line-clamp-none"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {VALID_ROLES.map((r) => (
                      <SelectItem key={r} value={r}>
                        <span className="flex items-center gap-2">
                          {ROLE_META[r].label}
                          <span className="text-muted-foreground text-xs">— {ROLE_META[r].desc}</span>
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
            <DialogFooter>
              <Button type="submit" disabled={checking} size="sm" className="gap-2">
                {checking && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Continue
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <form onSubmit={handleCreateUser}>
            <DialogHeader>
              <DialogTitle>Create new account</DialogTitle>
              <DialogDescription>
                No account found for <span className="font-medium text-foreground">{email}</span>. Set up their credentials below.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-1.5">
                <Label htmlFor="add-name" className="text-xs">Name</Label>
                <Input id="add-name" placeholder="Jane Smith" value={name} onChange={(e) => setName(e.target.value)} className="h-9" autoFocus />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="grid gap-1.5">
                  <Label htmlFor="add-pw" className="text-xs">Temporary password</Label>
                  <Input id="add-pw" type="password" placeholder="Min 8 chars" required value={password} onChange={(e) => setPassword(e.target.value)} className="h-9" />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="add-cpw" className="text-xs">Confirm</Label>
                  <Input id="add-cpw" type="password" placeholder="Re-enter" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className="h-9" />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">They will be required to change this password on first login.</p>
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
            <DialogFooter className="gap-2 sm:gap-0">
              <Button type="button" variant="outline" size="sm" onClick={() => { setStep("email"); setError(""); }}>
                Back
              </Button>
              <Button type="submit" disabled={saving} size="sm" className="gap-2">
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Create Account
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

interface PendingInvite {
  id: string;
  email: string;
  name: string | null;
  role: string;
  status: string;
  invited_at: string;
  expires_at: string;
}

interface OrgMembersProps {
  org: { id: string; name: string; members: OrgMember[] };
  currentUserId: string;
  isAdmin: boolean;
  onMembersChanged: () => void;
}

export default function OrgMembers({ org, currentUserId, isAdmin, onMembersChanged }: OrgMembersProps) {
  const [updating, setUpdating] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [permOpen, setPermOpen] = useState(false);
  const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
  const [cancellingInvite, setCancellingInvite] = useState<string | null>(null);

  const fetchPendingInvites = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const res = await fetch("/api/orgs/invitations");
      if (res.ok) {
        const data = await res.json();
        setPendingInvites((data.invitations || []).filter((i: PendingInvite) => i.status === "pending"));
      }
    } catch { /* silent */ }
  }, [isAdmin]);

  useEffect(() => {
    fetchPendingInvites();
  }, [fetchPendingInvites]);

  async function handleCancelInvite(inviteId: string) {
    setCancellingInvite(inviteId);
    try {
      const res = await fetch(`/api/orgs/invitations/${inviteId}/cancel`, { method: "POST" });
      if (res.ok) {
        toast({ title: "Invitation cancelled" });
        fetchPendingInvites();
      } else {
        const data = await res.json().catch(() => ({}));
        toast({ title: "Failed", description: data.error || "Could not cancel invitation", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed", description: "Could not reach server", variant: "destructive" });
    } finally {
      setCancellingInvite(null);
    }
  }

  const handleRoleChange = useCallback(
    async (targetUserId: string, newRole: string) => {
      setUpdating(targetUserId);
      const target = org.members.find((m) => m.id === targetUserId);
      try {
        const res = await fetch(`/api/admin/users/${targetUserId}/roles`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: newRole }),
        });
        if (res.ok) {
          toast({ title: `${target?.name || target?.email} is now ${newRole}` });
          onMembersChanged();
        } else {
          const data = await res.json().catch(() => ({}));
          toast({ title: "Failed", description: data.error || "Something went wrong", variant: "destructive" });
        }
      } catch {
        toast({ title: "Failed", description: "Could not reach server", variant: "destructive" });
      } finally {
        setUpdating(null);
      }
    },
    [org.members, onMembersChanged]
  );

  async function handleRemove(targetUserId: string) {
    const target = org.members.find((m) => m.id === targetUserId);
    setRemoving(targetUserId);
    try {
      const res = await fetch(`/api/orgs/current/members/${targetUserId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        toast({ title: `${target?.name || target?.email} removed` });
        onMembersChanged();
      }
    } catch {
      toast({ title: "Failed to remove", variant: "destructive" });
    } finally {
      setRemoving(null);
    }
  }

  function handleAddUserCreated() {
    onMembersChanged();
    fetchPendingInvites();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {org.members.length} member{org.members.length !== 1 ? "s" : ""}
        </p>
        {isAdmin && <AddUserDialog onCreated={handleAddUserCreated} />}
      </div>

      {/* Clean table — no heavy borders, just rows */}
      <div className="text-sm">
        <div className="grid grid-cols-[1fr_100px_100px_auto] gap-x-4 px-1 pb-2 text-xs text-muted-foreground font-medium border-b border-border">
          <span>Name</span>
          <span>Role</span>
          <span>Joined</span>
          <span className="w-8" />
        </div>

        {org.members.map((member) => (
          <div
            key={member.id}
            className="grid grid-cols-[1fr_100px_100px_auto] gap-x-4 items-center px-1 py-3 border-b border-border/40 last:border-0 group"
          >
            {/* Name + email */}
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="h-6 w-6 rounded-full bg-muted flex items-center justify-center text-[10px] font-medium text-muted-foreground flex-shrink-0">
                  {(member.name || member.email).charAt(0).toUpperCase()}
                </div>
                <span className="font-medium truncate">{member.name || member.email}</span>
                {member.id === currentUserId && (
                  <span className="text-[10px] text-muted-foreground/60 font-normal">you</span>
                )}
              </div>
              {member.name && (
                <p className="text-xs text-muted-foreground truncate ml-8">{member.email}</p>
              )}
            </div>

            {/* Role */}
            <div>
              {isAdmin && member.id !== currentUserId ? (
                <Select
                  value={member.role}
                  onValueChange={(val) => handleRoleChange(member.id, val)}
                  disabled={updating === member.id}
                >
                  <SelectTrigger className="w-24 h-7 text-xs border-transparent hover:border-border transition-colors">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {VALID_ROLES.map((r) => (
                      <SelectItem key={r} value={r} className="text-xs">{ROLE_META[r].label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <span className="text-xs text-muted-foreground capitalize">{member.role}</span>
              )}
            </div>

            {/* Joined */}
            <span className="text-xs text-muted-foreground tabular-nums">
              {member.createdAt
                ? new Date(member.createdAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                    year: "numeric",
                  })
                : "—"}
            </span>

            {/* Remove */}
            <div className="w-8 flex justify-end">
              {isAdmin && member.id !== currentUserId ? (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                  onClick={() => handleRemove(member.id)}
                  disabled={removing === member.id}
                >
                  {removing === member.id ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <UserMinus className="h-3 w-3" />
                  )}
                </Button>
              ) : null}
            </div>
          </div>
        ))}

        {org.members.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Users className="h-8 w-8 text-muted-foreground/30 mb-2" />
            <p className="text-sm text-muted-foreground">No members yet</p>
          </div>
        )}
      </div>

      {/* Pending invitations sent by admins */}
      {isAdmin && pendingInvites.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Mail className="h-3.5 w-3.5 text-muted-foreground" />
            <p className="text-sm font-medium text-muted-foreground">
              Pending invitations ({pendingInvites.length})
            </p>
          </div>
          <div className="text-sm">
            <div className="grid grid-cols-[1fr_80px_100px_auto] gap-x-4 px-1 pb-2 text-xs text-muted-foreground font-medium border-b border-border">
              <span>Email</span>
              <span>Role</span>
              <span>Sent</span>
              <span className="w-8" />
            </div>
            {pendingInvites.map((invite) => (
              <div
                key={invite.id}
                className="grid grid-cols-[1fr_80px_100px_auto] gap-x-4 items-center px-1 py-3 border-b border-border/40 last:border-0 group"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <div className="h-6 w-6 rounded-full bg-muted/50 border border-dashed border-border flex items-center justify-center text-[10px] font-medium text-muted-foreground flex-shrink-0">
                      {invite.email.charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0">
                      <span className="font-medium truncate block">{invite.name || invite.email}</span>
                      {invite.name && <p className="text-xs text-muted-foreground truncate">{invite.email}</p>}
                    </div>
                  </div>
                </div>
                <span className="text-xs text-muted-foreground capitalize">{invite.role}</span>
                <div className="flex items-center gap-1 text-xs text-muted-foreground tabular-nums">
                  <Clock className="h-3 w-3" />
                  {invite.invited_at
                    ? new Date(invite.invited_at).toLocaleDateString(undefined, {
                        month: "short",
                        day: "numeric",
                      })
                    : "—"}
                </div>
                <div className="w-8 flex justify-end">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                    onClick={() => handleCancelInvite(invite.id)}
                    disabled={cancellingInvite === invite.id}
                    title="Cancel invitation"
                  >
                    {cancellingInvite === invite.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <X className="h-3 w-3" />
                    )}
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Permissions reference — collapsed by default */}
      <Collapsible open={permOpen} onOpenChange={setPermOpen}>
        <CollapsibleTrigger asChild>
          <button className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors py-1">
            <ChevronDown className={`h-3 w-3 transition-transform ${permOpen ? "rotate-180" : ""}`} />
            Permissions reference
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-3 text-xs">
            <div className="grid grid-cols-[1fr_60px_60px_60px] gap-x-2 pb-2 font-medium text-muted-foreground border-b border-border">
              <span />
              <span className="text-center">View</span>
              <span className="text-center">Edit</span>
              <span className="text-center">Admin</span>
            </div>
            {PERMISSION_TABLE.map((section) => (
              <Fragment key={section.category}>
                <div className="pt-3 pb-1 text-[10px] font-semibold text-muted-foreground/60 uppercase tracking-widest">
                  {section.category}
                </div>
                {section.features.map((feat) => (
                  <div key={feat.name} className="grid grid-cols-[1fr_60px_60px_60px] gap-x-2 py-1.5 border-b border-border/30 text-muted-foreground">
                    <span>{feat.name}</span>
                    <span className="flex justify-center">{feat.viewer ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                    <span className="flex justify-center">{feat.editor ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                    <span className="flex justify-center">{feat.admin ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                  </div>
                ))}
              </Fragment>
            ))}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
