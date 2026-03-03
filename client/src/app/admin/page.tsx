"use client";

import { useEffect, useState, useCallback } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { useRouter } from "next/navigation";
import { toast } from "@/hooks/use-toast";
import {
  Shield,
  Users,
  Eye,
  Pencil,
  Crown,
  Copy,
  Check,
  Minus,
  X,
  ChevronRight,
  ChevronLeft,
  UserPlus,
  Plus,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface UserRow {
  id: string;
  email: string;
  name: string | null;
  role: string;
  created_at: string | null;
}

const VALID_ROLES = ["admin", "editor", "viewer"] as const;

const ROLE_INFO = {
  viewer: {
    icon: Eye,
    color: "text-blue-500",
    bg: "bg-blue-500/10",
    badge: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200",
    label: "Viewer",
    summary: "Read-only access",
  },
  editor: {
    icon: Pencil,
    color: "text-amber-500",
    bg: "bg-amber-500/10",
    badge: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200",
    label: "Editor",
    summary: "Read + write access",
  },
  admin: {
    icon: Crown,
    color: "text-purple-500",
    bg: "bg-purple-500/10",
    badge: "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200",
    label: "Admin",
    summary: "Full access + user management",
  },
} as const;

type PermAccess = true | false;

const PERMISSION_TABLE: { category: string; features: { name: string; viewer: PermAccess; editor: PermAccess; admin: PermAccess }[] }[] = [
  {
    category: "Incidents",
    features: [
      { name: "View incidents & alerts",   viewer: true,  editor: true,  admin: true },
      { name: "Update & resolve incidents", viewer: false, editor: true,  admin: true },
      { name: "Apply suggestions & merge",  viewer: false, editor: true,  admin: true },
    ],
  },
  {
    category: "Postmortems",
    features: [
      { name: "View postmortems",           viewer: true,  editor: true,  admin: true },
      { name: "Edit & export postmortems",  viewer: false, editor: true,  admin: true },
    ],
  },
  {
    category: "Chat & Knowledge Base",
    features: [
      { name: "Use the chat assistant",     viewer: true,  editor: true,  admin: true },
      { name: "View knowledge base",        viewer: true,  editor: true,  admin: true },
      { name: "Upload & manage documents",  viewer: false, editor: true,  admin: true },
    ],
  },
  {
    category: "Integrations",
    features: [
      { name: "View connector status",      viewer: true,  editor: true,  admin: true },
      { name: "Connect & disconnect",       viewer: false, editor: true,  admin: true },
      { name: "Manage SSH keys & VMs",      viewer: false, editor: true,  admin: true },
    ],
  },
  {
    category: "Infrastructure",
    features: [
      { name: "View service graph",         viewer: true,  editor: true,  admin: true },
      { name: "Edit services & dependencies", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Administration",
    features: [
      { name: "Configure LLM providers",    viewer: false, editor: false, admin: true },
      { name: "Manage users & roles",       viewer: false, editor: false, admin: true },
    ],
  },
];

function PermCell({ allowed }: { allowed: boolean }) {
  return allowed ? (
    <Check className="h-4 w-4 text-green-500" />
  ) : (
    <Minus className="h-4 w-4 text-muted-foreground/30" />
  );
}

function RoleComparisonTable() {
  return (
    <div className="rounded-lg border border-border overflow-hidden text-sm">
      <table className="w-full">
        <thead>
          <tr className="bg-muted/50">
            <th className="text-left px-3 py-2.5 font-medium text-muted-foreground w-[55%]">Feature</th>
            {(["viewer", "editor", "admin"] as const).map((role) => {
              const info = ROLE_INFO[role];
              const Icon = info.icon;
              return (
                <th key={role} className="text-center px-2 py-2.5 font-medium w-[15%]">
                  <div className="flex flex-col items-center gap-1">
                    <Icon className={`h-4 w-4 ${info.color}`} />
                    <span className="text-xs">{info.label}</span>
                  </div>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {PERMISSION_TABLE.map((section) => (
            <>
              <tr key={section.category}>
                <td
                  colSpan={4}
                  className="px-3 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider bg-muted/30 border-t border-border"
                >
                  {section.category}
                </td>
              </tr>
              {section.features.map((feat) => (
                <tr key={feat.name} className="border-t border-border/50">
                  <td className="px-3 py-2 text-foreground/80">{feat.name}</td>
                  <td className="text-center px-2 py-2"><PermCell allowed={feat.viewer} /></td>
                  <td className="text-center px-2 py-2"><PermCell allowed={feat.editor} /></td>
                  <td className="text-center px-2 py-2"><PermCell allowed={feat.admin} /></td>
                </tr>
              ))}
            </>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const WIZARD_STORAGE_KEY = "aurora_admin_wizard_dismissed";

function OnboardingWizard({ onDismiss }: { onDismiss: () => void }) {
  const [step, setStep] = useState(0);
  const [copied, setCopied] = useState(false);

  const signUpUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/sign-up`
      : "/sign-up";

  function copyLink() {
    navigator.clipboard.writeText(signUpUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const steps = [
    {
      title: "Welcome, Admin",
      icon: Shield,
      content: (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground leading-relaxed">
            You&apos;re the first user, so you&apos;ve been made the admin.
            Aurora uses <strong className="text-foreground">role-based access control</strong> with
            three tiers. Each role inherits everything from the one below it.
          </p>
          <RoleComparisonTable />
        </div>
      ),
    },
    {
      title: "Invite Your Team",
      icon: UserPlus,
      content: (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground leading-relaxed">
            Share this sign-up link with your team. Anyone who registers will
            start as a <Badge variant="secondary" className="text-xs">Viewer</Badge> by default.
          </p>
          <div className="flex items-center gap-2 p-3 bg-muted rounded-lg border border-border">
            <code className="text-sm flex-1 truncate select-all">{signUpUrl}</code>
            <Button variant="ghost" size="sm" className="h-8 w-8 p-0 flex-shrink-0" onClick={copyLink}>
              {copied ? (
                <Check className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
          </div>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Once they&apos;ve signed up, you&apos;ll see them in the user table below.
            You can then change their role to <strong className="text-foreground">Editor</strong> or{" "}
            <strong className="text-foreground">Admin</strong> from the dropdown.
          </p>
          <div className="rounded-lg border border-border overflow-hidden mt-2">
            <div className="bg-muted/50 px-4 py-2.5 flex items-center gap-3 text-sm">
              <span className="w-32 text-muted-foreground font-medium">jane@team.com</span>
              <span className="flex-1 text-muted-foreground">Jane Smith</span>
              <div className="flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-background text-xs">
                <Eye className="h-3 w-3 text-blue-500" />
                viewer
              </div>
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
              <div className="flex items-center gap-1.5 px-2 py-1 rounded border border-amber-300 bg-amber-500/10 text-xs font-medium">
                <Pencil className="h-3 w-3 text-amber-500" />
                editor
              </div>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Select a new role from the dropdown to change it instantly. Enforcement is server-side,
            so even direct API calls respect the assigned role.
          </p>
        </div>
      ),
    },
  ];

  const current = steps[step];
  const StepIcon = current.icon;

  return (
    <Card className="mb-8 border-primary/30 shadow-sm">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-lg bg-primary/10 flex items-center justify-center">
              <StepIcon className="h-5 w-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-lg">{current.title}</CardTitle>
              <CardDescription>
                Step {step + 1} of {steps.length}
              </CardDescription>
            </div>
          </div>
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0" onClick={onDismiss}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex gap-1.5 mt-3">
          {steps.map((_, i) => (
            <div
              key={i}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i <= step ? "bg-primary" : "bg-muted"
              }`}
            />
          ))}
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {current.content}
        <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setStep(step - 1)}
            disabled={step === 0}
            className="gap-1"
          >
            <ChevronLeft className="h-4 w-4" />
            Back
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={onDismiss} className="text-muted-foreground">
              Dismiss
            </Button>
            {step < steps.length - 1 ? (
              <Button size="sm" onClick={() => setStep(step + 1)} className="gap-1">
                Next
                <ChevronRight className="h-4 w-4" />
              </Button>
            ) : (
              <Button size="sm" onClick={onDismiss} className="gap-1">
                Got it
                <Check className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function RoleBadge({ role }: { role: string }) {
  const info = ROLE_INFO[role as keyof typeof ROLE_INFO];
  if (!info) return <Badge variant="outline">{role}</Badge>;
  const Icon = info.icon;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${info.badge}`}>
      <Icon className="h-3 w-3" />
      {info.label}
    </span>
  );
}

function AddUserDialog({ onCreated }: { onCreated: (user: UserRow) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [role, setRole] = useState<string>("viewer");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  function reset() {
    setName("");
    setEmail("");
    setPassword("");
    setConfirmPassword("");
    setRole("viewer");
    setError("");
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!email || !password) {
      setError("Email and password are required");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setSaving(true);
    try {
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, name, role }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || "Failed to create user");
        return;
      }
      onCreated(data);
      const roleLabel = ROLE_INFO[role as keyof typeof ROLE_INFO]?.label || role;
      toast({
        title: "User created",
        description: `${name || email} added as ${roleLabel}`,
        duration: 5000,
      });
      reset();
      setOpen(false);
    } catch {
      setError("Something went wrong");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button size="icon" variant="outline" className="h-9 w-9">
          <Plus className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleCreate}>
          <DialogHeader>
            <DialogTitle>Add user</DialogTitle>
            <DialogDescription>
              Create an account for a team member. They can sign in with these
              credentials.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="add-name">Name</Label>
              <Input
                id="add-name"
                placeholder="Jane Smith"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-email">Email</Label>
              <Input
                id="add-email"
                type="email"
                placeholder="jane@company.com"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-password">Password</Label>
              <Input
                id="add-password"
                type="password"
                placeholder="Min 8 characters"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-confirm-password">Confirm password</Label>
              <Input
                id="add-confirm-password"
                type="password"
                placeholder="Re-enter password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Role</Label>
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {VALID_ROLES.map((r) => {
                    const info = ROLE_INFO[r];
                    const Icon = info.icon;
                    return (
                      <SelectItem key={r} value={r}>
                        <span className="flex items-center gap-2">
                          <Icon className={`h-3.5 w-3.5 ${info.color}`} />
                          {info.label}
                        </span>
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
            </div>
            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={saving} className="gap-2">
              {saving && <Loader2 className="h-4 w-4 animate-spin" />}
              Create user
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function AdminPage() {
  const { user, isLoaded } = useUser();
  const router = useRouter();
  const [users, setUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState<string | null>(null);
  const [showWizard, setShowWizard] = useState(false);

  const userRole = user?.role;

  useEffect(() => {
    if (isLoaded && userRole !== "admin") {
      router.replace("/");
    }
  }, [isLoaded, userRole, router]);

  useEffect(() => {
    if (userRole === "admin") {
      fetchUsers();
      const dismissed = localStorage.getItem(WIZARD_STORAGE_KEY);
      if (!dismissed) {
        setShowWizard(true);
      }
    }
  }, [userRole]);

  const dismissWizard = useCallback(() => {
    setShowWizard(false);
    localStorage.setItem(WIZARD_STORAGE_KEY, "true");
  }, []);

  async function fetchUsers() {
    try {
      const res = await fetch("/api/admin/users");
      if (res.ok) {
        setUsers(await res.json());
      }
    } catch (err) {
      console.error("Failed to load users:", err);
    } finally {
      setLoading(false);
    }
  }

  async function handleRoleChange(targetUserId: string, newRole: string) {
    setUpdating(targetUserId);
    const targetUser = users.find((u) => u.id === targetUserId);
    const oldRole = targetUser?.role;
    try {
      const res = await fetch(`/api/admin/users/${targetUserId}/roles`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: newRole }),
      });
      if (res.ok) {
        setUsers((prev) =>
          prev.map((u) => (u.id === targetUserId ? { ...u, role: newRole } : u))
        );
        const displayName = targetUser?.name || targetUser?.email || "User";
        const oldLabel = oldRole ? ROLE_INFO[oldRole as keyof typeof ROLE_INFO]?.label || oldRole : "unknown";
        const newLabel = ROLE_INFO[newRole as keyof typeof ROLE_INFO]?.label || newRole;
        toast({
          title: "Role updated",
          description: `${displayName} changed from ${oldLabel} to ${newLabel}`,
          duration: 5000,
        });
      } else {
        const data = await res.json().catch(() => ({}));
        toast({
          title: "Failed to update role",
          description: data.error || "Something went wrong",
          variant: "destructive",
        });
      }
    } catch (err) {
      console.error("Failed to update role:", err);
      toast({
        title: "Failed to update role",
        description: "Could not reach the server",
        variant: "destructive",
      });
    } finally {
      setUpdating(null);
    }
  }

  if (!isLoaded || user?.role !== "admin") {
    return null;
  }

  return (
    <div className="max-w-4xl mx-auto p-8">
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-3">
          <Shield className="h-7 w-7 text-primary" />
          <div>
            <h1 className="text-3xl font-bold">User Management</h1>
            <p className="text-sm text-muted-foreground mt-0.5">
              Manage your team&apos;s access to Aurora
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <AddUserDialog
            onCreated={(newUser) =>
              setUsers((prev) => [...prev, newUser])
            }
          />
          {!showWizard && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowWizard(true)}
              className="gap-2 text-muted-foreground"
            >
              <Users className="h-4 w-4" />
              Setup guide
            </Button>
          )}
        </div>
      </div>

      {showWizard && <OnboardingWizard onDismiss={dismissWizard} />}

      {loading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          Loading users...
        </div>
      ) : users.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-center">
            <Users className="h-12 w-12 text-muted-foreground/40 mb-4" />
            <h3 className="text-lg font-medium mb-1">No other users yet</h3>
            <p className="text-sm text-muted-foreground max-w-sm">
              Share your sign-up link with your team to get started. New users
              will appear here as Viewers, and you can promote them.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="border border-border rounded-lg overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="bg-muted/50 border-b border-border">
                <th className="text-left px-4 py-3 text-sm font-medium text-muted-foreground">
                  User
                </th>
                <th className="text-left px-4 py-3 text-sm font-medium text-muted-foreground">
                  Role
                </th>
                <th className="text-left px-4 py-3 text-sm font-medium text-muted-foreground">
                  Joined
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-border last:border-0 hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">
                    <div className="flex flex-col">
                      <span className="text-sm font-medium">
                        {u.name || u.email}
                        {u.id === user?.id && (
                          <span className="ml-2 text-xs text-muted-foreground">(you)</span>
                        )}
                      </span>
                      {u.name && (
                        <span className="text-xs text-muted-foreground">{u.email}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    {u.id === user?.id ? (
                      <RoleBadge role={u.role} />
                    ) : (
                      <Select
                        value={u.role}
                        onValueChange={(val) => handleRoleChange(u.id, val)}
                        disabled={updating === u.id}
                      >
                        <SelectTrigger className="w-36 h-8 text-sm">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {VALID_ROLES.map((r) => {
                            const info = ROLE_INFO[r];
                            const Icon = info.icon;
                            return (
                              <SelectItem key={r} value={r} className="text-sm">
                                <span className="flex items-center gap-2">
                                  <Icon className={`h-3.5 w-3.5 ${info.color}`} />
                                  {info.label}
                                </span>
                              </SelectItem>
                            );
                          })}
                        </SelectContent>
                      </Select>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-foreground">
                    {u.created_at
                      ? new Date(u.created_at).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
