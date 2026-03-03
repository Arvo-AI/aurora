"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Lock, Plus, X, Save, Loader2, Mail } from "lucide-react";
import { toast } from "@/hooks/use-toast";

interface OrgPreferencesProps {
  isAdmin: boolean;
}

export default function OrgPreferences({ isAdmin }: OrgPreferencesProps) {
  const [prefs, setPrefs] = useState<Record<string, string | string[]>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [emailInput, setEmailInput] = useState("");
  const [emails, setEmails] = useState<string[]>([]);

  useEffect(() => {
    fetch("/api/orgs/preferences")
      .then((r) => r.json())
      .then((data) => {
        setPrefs(data);
        if (Array.isArray(data.notification_emails)) {
          setEmails(data.notification_emails);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  function addEmail() {
    const email = emailInput.trim();
    if (!email || emails.includes(email)) return;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      toast({ title: "Invalid email", variant: "destructive" });
      return;
    }
    setEmails((prev) => [...prev, email]);
    setEmailInput("");
  }

  function removeEmail(email: string) {
    setEmails((prev) => prev.filter((e) => e !== email));
  }

  async function handleSave() {
    setSaving(true);
    try {
      const res = await fetch("/api/orgs/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...prefs,
          notification_emails: emails,
        }),
      });
      if (res.ok) {
        toast({ title: "Preferences saved" });
      } else {
        toast({ title: "Failed to save", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to save", variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading preferences...
      </div>
    );
  }

  const canEdit = isAdmin;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Organization Preferences</h2>
          <p className="text-sm text-muted-foreground">
            Settings that apply to all members of this organization
          </p>
        </div>
        {!canEdit && (
          <Badge variant="secondary" className="gap-1">
            <Lock className="h-3 w-3" />
            View only
          </Badge>
        )}
      </div>

      {/* Notification emails */}
      <Card className="border-border/50">
        <CardContent className="p-5 space-y-4">
          <div className="flex items-center gap-2">
            <Mail className="h-4 w-4 text-muted-foreground" />
            <div>
              <h3 className="text-sm font-medium">RCA Notification Emails</h3>
              <p className="text-xs text-muted-foreground">
                Receive email notifications when new Root Cause Analysis reports are generated
              </p>
            </div>
          </div>

          {canEdit && (
            <div className="flex items-center gap-2">
              <Input
                placeholder="team@company.com"
                value={emailInput}
                onChange={(e) => setEmailInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addEmail(); } }}
                className="max-w-sm"
              />
              <Button variant="outline" size="sm" onClick={addEmail} className="gap-1 flex-shrink-0">
                <Plus className="h-3.5 w-3.5" />
                Add
              </Button>
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            {emails.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">No notification emails configured</p>
            ) : (
              emails.map((email) => (
                <Badge key={email} variant="secondary" className="gap-1.5 py-1 px-2.5">
                  {email}
                  {canEdit && (
                    <button onClick={() => removeEmail(email)} className="hover:text-destructive transition-colors">
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </Badge>
              ))
            )}
          </div>
        </CardContent>
      </Card>

      {/* Save button */}
      {canEdit && (
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={saving} className="gap-2">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save Preferences
          </Button>
        </div>
      )}
    </div>
  );
}
