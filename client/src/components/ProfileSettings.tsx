"use client"

import { useUser } from "@/hooks/useAuthHooks"
import { useState, useEffect } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Eye, Pencil, Crown, Shield, Mail } from "lucide-react"
import { ROLE_ADMIN } from "@/lib/roles"

const ROLE_INFO = {
  viewer: {
    icon: Eye,
    label: "Viewer",
    color: "text-blue-500",
    badgeClass: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200",
    description: "Read-only access to incidents, postmortems, dashboards, and chat. Cannot connect integrations, upload documents, or manage users.",
  },
  editor: {
    icon: Pencil,
    label: "Editor",
    color: "text-amber-500",
    badgeClass: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200",
    description: "Everything a Viewer can do, plus connect integrations, manage documents and knowledge base, edit incidents and postmortems, and manage SSH keys and VMs.",
  },
  admin: {
    icon: Crown,
    label: "Admin",
    color: "text-purple-500",
    badgeClass: "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200",
    description: "Full access. Everything an Editor can do, plus manage users and roles, and configure LLM providers.",
  },
} as const;

interface AdminUser {
  name: string | null;
  email: string;
}

export function ProfileSettings() {
  const { user, isLoaded } = useUser()
  const [isChangingPassword, setIsChangingPassword] = useState(false)
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [error, setError] = useState("")
  const [success, setSuccess] = useState("")
  const [isLoading, setIsLoading] = useState(false)
  const [admins, setAdmins] = useState<AdminUser[]>([])

  useEffect(() => {
    if (user?.role !== ROLE_ADMIN) {
      fetch("/api/auth/admins")
        .then((res) => (res.ok ? res.json() : []))
        .then((users: { name: string | null; email: string }[]) => {
          setAdmins(users);
        })
        .catch(() => {});
    }
  }, [user?.role]);

  if (!isLoaded) {
    return <div className="text-muted-foreground">Loading...</div>
  }

  if (!user) {
    return <div className="text-muted-foreground">Not signed in</div>
  }

  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")
    setSuccess("")

    if (newPassword !== confirmPassword) {
      setError("New passwords do not match")
      return
    }

    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters")
      return
    }

    setIsLoading(true)

    try {
      const response = await fetch('/api/auth/change-password', {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          currentPassword,
          newPassword,
        }),
      })

      if (!response.ok) {
        const data = await response.json()
        setError(data.error || "Failed to change password")
        return
      }

      setSuccess("Password changed successfully")
      setCurrentPassword("")
      setNewPassword("")
      setConfirmPassword("")
      setIsChangingPassword(false)
    } catch (err) {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Profile Information */}
      <div className="space-y-4">
        <div>
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            value={user.email || ""}
            disabled
            className="bg-muted"
          />
          <p className="text-sm text-muted-foreground mt-1">
            Your email address cannot be changed
          </p>
        </div>

        <div>
          <Label htmlFor="name">Name</Label>
          <Input
            id="name"
            type="text"
            value={user.fullName || ""}
            disabled
            className="bg-muted"
          />
        </div>

        <div>
          <Label htmlFor="user-id">User ID</Label>
          <Input
            id="user-id"
            type="text"
            value={user.id}
            disabled
            className="bg-muted font-mono text-xs"
          />
        </div>
      </div>

      {/* Role Section */}
      <div className="border-t pt-6">
        <div className="flex items-center gap-2 mb-4">
          <Shield className="h-5 w-5 text-muted-foreground" />
          <h3 className="text-lg font-semibold">Your Role</h3>
        </div>
        {(() => {
          const role = (user.role || "viewer") as keyof typeof ROLE_INFO;
          const info = ROLE_INFO[role] || ROLE_INFO.viewer;
          const Icon = info.icon;
          return (
            <div className="rounded-lg border p-4 space-y-3">
              <div className="flex items-center gap-2">
                <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-sm font-medium ${info.badgeClass}`}>
                  <Icon className="h-4 w-4" />
                  {info.label}
                </span>
              </div>
              <p className="text-sm text-muted-foreground leading-relaxed">
                {info.description}
              </p>
              {user.role !== ROLE_ADMIN && admins.length > 0 && (
                <div className="pt-2 border-t">
                  <p className="text-xs text-muted-foreground mb-1.5">
                    To request a role change, contact an admin:
                  </p>
                  <div className="space-y-1">
                    {admins.map((admin) => (
                      <div key={admin.email} className="flex items-center gap-2 text-sm">
                        <Mail className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="font-medium">{admin.name || admin.email}</span>
                        {admin.name && (
                          <span className="text-muted-foreground">({admin.email})</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })()}
      </div>

      {/* Password Change Section */}
      <div className="border-t pt-6">
        <h3 className="text-lg font-semibold mb-4">Change Password</h3>

        {!isChangingPassword ? (
          <Button onClick={() => setIsChangingPassword(true)} variant="outline">
            Change Password
          </Button>
        ) : (
          <form onSubmit={handlePasswordChange} className="space-y-4">
            <div>
              <Label htmlFor="current-password">Current Password</Label>
              <Input
                id="current-password"
                type="password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
                disabled={isLoading}
              />
            </div>

            <div>
              <Label htmlFor="new-password">New Password</Label>
              <Input
                id="new-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                disabled={isLoading}
                placeholder="Min 8 characters"
              />
            </div>

            <div>
              <Label htmlFor="confirm-password">Confirm New Password</Label>
              <Input
                id="confirm-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                disabled={isLoading}
              />
            </div>

            {error && (
              <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-3">
                <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
              </div>
            )}

            {success && (
              <div className="rounded-md bg-green-50 dark:bg-green-900/20 p-3">
                <p className="text-sm text-green-800 dark:text-green-200">{success}</p>
              </div>
            )}

            <div className="flex gap-2">
              <Button type="submit" disabled={isLoading}>
                {isLoading ? "Changing..." : "Change Password"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setIsChangingPassword(false)
                  setCurrentPassword("")
                  setNewPassword("")
                  setConfirmPassword("")
                  setError("")
                  setSuccess("")
                }}
                disabled={isLoading}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
