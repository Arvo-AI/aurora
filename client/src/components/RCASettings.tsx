"use client";

import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";
import { Bell, Plus, Trash2, Check, Clock, RefreshCw, Mail, Zap } from "lucide-react";
import { useAuth } from "@/hooks/useAuthHooks";
import { canWrite } from "@/lib/roles";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  listRCAEmails,
  addRCAEmail,
  verifyRCAEmail,
  resendVerificationCode,
  removeRCAEmail,
  toggleRCAEmail,
  type RCAEmail,
} from "@/lib/services/rcaEmails";
import { NotificationToggle } from "@/components/NotificationToggle";

export function RCASettings() {
  const [preferences, setPreferences] = useState({
    rca_email_notifications: false,
    rca_email_start_notifications: false,
    action_email_notifications: false,
  });
  const [savingPreferences, setSavingPreferences] = useState<Record<string, boolean>>({});
  const [isLoadingNotificationPref, setIsLoadingNotificationPref] = useState(true);

  const [orgEmails, setOrgEmails] = useState<RCAEmail[]>([]);
  const [isLoadingEmails, setIsLoadingEmails] = useState(true);

  // Add email state
  const [isAddDialogOpen, setIsAddDialogOpen] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [isAddingEmail, setIsAddingEmail] = useState(false);

  // Verification dialog state
  const [isVerifyDialogOpen, setIsVerifyDialogOpen] = useState(false);
  const [verifyingEmail, setVerifyingEmail] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [isVerifying, setIsVerifying] = useState(false);
  const [isResending, setIsResending] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(0);

  const { toast } = useToast();
  const { userId, role } = useAuth();
  const canEditNotifications = canWrite(role);

  useEffect(() => {
    const loadNotificationPreferences = async () => {
      if (!userId) return;

      try {
        const keys = ['rca_email_notifications', 'rca_email_start_notifications', 'action_email_notifications'];
        const loaded: Record<string, boolean> = {};

        await Promise.all(keys.map(async (key) => {
          const response = await fetch(
            `/api/proxy/user-preferences?key=${key}`,
          );

          if (response.ok) {
            const data = await response.json();
            if (data.value !== null && data.value !== undefined) {
              const value = typeof data.value === 'boolean' ? data.value : data.value === 'true' || data.value === true;
              loaded[key] = value;
            }
          }
        }));

        setPreferences(prev => ({ ...prev, ...loaded }));
      } catch (error) {
        console.error("Error loading notification preferences:", error);
      } finally {
        setIsLoadingNotificationPref(false);
      }
    };

    loadNotificationPreferences();
  }, [userId]);

  useEffect(() => {
    loadEmails();
  }, [userId]);

  useEffect(() => {
    if (resendCooldown > 0) {
      const timer = setTimeout(() => setResendCooldown(resendCooldown - 1), 1000);
      return () => clearTimeout(timer);
    }
  }, [resendCooldown]);

  const loadEmails = useCallback(async () => {
    if (!userId) return;

    try {
      setIsLoadingEmails(true);
      const data = await listRCAEmails();
      setOrgEmails(data.emails);
    } catch (error) {
      console.error("Error loading emails:", error);
      toast({
        title: "Error",
        description: "Failed to load email addresses",
        variant: "destructive",
      });
    } finally {
      setIsLoadingEmails(false);
    }
  }, [userId, toast]);

  const handlePreferenceChange = async (key: string, enabled: boolean) => {
    if (!userId) return;

    setPreferences(prev => ({ ...prev, [key]: enabled }));
    setSavingPreferences(prev => ({ ...prev, [key]: true }));

    try {
      const response = await fetch(
        `/api/proxy/user-preferences`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key, value: enabled }),
        }
      );

      if (!response.ok) {
        throw new Error('Failed to save preference');
      }
    } catch (error) {
      console.error(`Error saving ${key} preference:`, error);
      setPreferences(prev => ({ ...prev, [key]: !enabled }));
      toast({
        title: "Error",
        description: "Failed to save notification preference",
        variant: "destructive",
      });
    } finally {
      setSavingPreferences(prev => ({ ...prev, [key]: false }));
    }
  };

  const handleAddEmail = async () => {
    if (!userId) return;
    if (!newEmail.trim()) {
      toast({ title: "Error", description: "Please enter an email address", variant: "destructive" });
      return;
    }

    const emailRegex = /^[^\s@]+@[^\s@.]+\.[^\s@]+$/;
    if (newEmail.length > 320 || !emailRegex.test(newEmail)) {
      toast({ title: "Error", description: "Please enter a valid email address", variant: "destructive" });
      return;
    }

    try {
      setIsAddingEmail(true);
      await addRCAEmail(newEmail.trim().toLowerCase());

      toast({ title: "Verification Code Sent", description: `A verification code has been sent to ${newEmail}` });

      setIsAddDialogOpen(false);
      setVerifyingEmail(newEmail.trim().toLowerCase());
      setIsVerifyDialogOpen(true);
      setNewEmail("");

      await loadEmails();
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to add email", variant: "destructive" });
    } finally {
      setIsAddingEmail(false);
    }
  };

  const handleVerifyEmail = async () => {
    if (!userId) return;
    if (!verificationCode.trim() || verificationCode.trim().length !== 6) {
      toast({ title: "Error", description: "Please enter a valid 6-digit code", variant: "destructive" });
      return;
    }

    try {
      setIsVerifying(true);
      await verifyRCAEmail(verifyingEmail, verificationCode.trim());

      toast({ title: "Email Verified", description: "Your email has been verified successfully" });

      setIsVerifyDialogOpen(false);
      setVerificationCode("");
      setVerifyingEmail("");
      await loadEmails();
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to verify email", variant: "destructive" });
    } finally {
      setIsVerifying(false);
    }
  };

  const handleResendCode = async () => {
    if (!userId) return;
    if (resendCooldown > 0) return;

    try {
      setIsResending(true);
      await resendVerificationCode(verifyingEmail);
      toast({ title: "Code Resent", description: "A new verification code has been sent" });
      setResendCooldown(60);
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to resend code", variant: "destructive" });
    } finally {
      setIsResending(false);
    }
  };

  const handleToggleEmail = async (emailId: number, currentStatus: boolean, emailAddress: string) => {
    if (!userId) return;

    try {
      await toggleRCAEmail(emailId, !currentStatus);
      toast({
        title: !currentStatus ? "Email Enabled" : "Email Disabled",
        description: `${emailAddress} will ${!currentStatus ? 'now' : 'no longer'} receive notifications`,
      });
      await loadEmails();
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to toggle email", variant: "destructive" });
    }
  };

  const handleRemoveEmail = async (emailId: number, emailAddress: string) => {
    if (!userId) return;
    if (!confirm(`Remove ${emailAddress} from notifications?`)) return;

    try {
      await removeRCAEmail(emailId);
      toast({ title: "Email Removed", description: "Email address has been removed" });
      await loadEmails();
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to remove email", variant: "destructive" });
    }
  };

  const openVerifyDialog = (email: string) => {
    setVerifyingEmail(email);
    setVerificationCode("");
    setIsVerifyDialogOpen(true);
  };

  return (
    <div className="space-y-6 min-h-0">
      {/* Email Recipients Card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Mail className="h-5 w-5" />
            Notification Recipients
          </CardTitle>
          <CardDescription>
            Manage who receives email notifications for investigations and actions
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted-foreground">
                All org members who trigger actions or investigations will notify these recipients
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setIsAddDialogOpen(true)}
                className="flex items-center gap-2"
                disabled={!canEditNotifications}
              >
                <Plus className="h-4 w-4" />
                Add Email
              </Button>
            </div>

            {isLoadingEmails ? (
              <div className="flex items-center justify-center p-6">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
              </div>
            ) : orgEmails.length === 0 ? (
              <div className="text-sm text-muted-foreground p-4 border border-dashed rounded-lg text-center">
                No additional email addresses added
              </div>
            ) : (
              <div className="space-y-2">
                {orgEmails.map((email) => (
                  <div
                    key={email.id}
                    className="flex items-center justify-between p-3 border rounded-lg transition-colors hover:bg-muted/50"
                  >
                    <div className="flex items-center gap-3 flex-1 min-w-0">
                      <span className="text-sm truncate">{email.email}</span>
                      {email.is_verified ? (
                        <Badge variant="default" className="bg-green-600 shrink-0 flex items-center gap-1">
                          <Check className="h-3 w-3" />
                          Verified
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="shrink-0 flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          Pending
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {email.is_verified && (
                        <Switch
                          checked={email.is_enabled}
                          onCheckedChange={() => handleToggleEmail(email.id, email.is_enabled, email.email)}
                          disabled={!canEditNotifications}
                        />
                      )}
                      {!email.is_verified && canEditNotifications && (
                        <Button variant="ghost" size="sm" onClick={() => openVerifyDialog(email.email)}>
                          Verify
                        </Button>
                      )}
                      {canEditNotifications && (
                        <Button variant="ghost" size="sm" onClick={() => handleRemoveEmail(email.id, email.email)}>
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* RCA Investigation Notifications */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Bell className="h-5 w-5" />
            Investigation Notifications
          </CardTitle>
          <CardDescription>
            Get notified when Aurora completes root cause analysis investigations
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <NotificationToggle
            title="Investigation Complete"
            description="Receive an email when Aurora finishes an RCA investigation"
            icon={<Bell className="h-4 w-4" />}
            checked={preferences.rca_email_notifications}
            onChange={(checked) => handlePreferenceChange('rca_email_notifications', checked)}
            isLoading={isLoadingNotificationPref || savingPreferences.rca_email_notifications}
            disabled={!canEditNotifications}
          />

          <NotificationToggle
            title="Investigation Started"
            description="Also receive an email when Aurora begins an investigation"
            icon={<Bell className="h-4 w-4" />}
            checked={preferences.rca_email_start_notifications}
            onChange={(checked) => handlePreferenceChange('rca_email_start_notifications', checked)}
            isLoading={isLoadingNotificationPref || savingPreferences.rca_email_start_notifications}
            disabled={!canEditNotifications}
          />
        </CardContent>
      </Card>

      {/* Action Notifications */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Zap className="h-5 w-5" />
            Action Notifications
          </CardTitle>
          <CardDescription>
            Get notified when automated actions complete or fail
          </CardDescription>
        </CardHeader>
        <CardContent>
          <NotificationToggle
            title="Action Complete"
            description="Receive an email when an Aurora Action finishes running"
            icon={<Zap className="h-4 w-4" />}
            checked={preferences.action_email_notifications}
            onChange={(checked) => handlePreferenceChange('action_email_notifications', checked)}
            isLoading={isLoadingNotificationPref || savingPreferences.action_email_notifications}
            disabled={!canEditNotifications}
          />
        </CardContent>
      </Card>

      {/* Add Email Dialog */}
      <Dialog open={isAddDialogOpen} onOpenChange={setIsAddDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Email Recipient</DialogTitle>
            <DialogDescription>
              Add an email address to receive notifications. A verification code will be sent to confirm.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="new-email">Email Address</Label>
              <Input
                id="new-email"
                type="email"
                placeholder="email@example.com"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAddEmail()}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsAddDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleAddEmail} disabled={isAddingEmail}>
              {isAddingEmail ? "Sending..." : "Send Verification Code"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Verify Email Dialog */}
      <Dialog open={isVerifyDialogOpen} onOpenChange={setIsVerifyDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Verify Email Address</DialogTitle>
            <DialogDescription>
              Enter the 6-digit verification code sent to {verifyingEmail}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="verification-code">Verification Code</Label>
              <Input
                id="verification-code"
                type="text"
                placeholder="000000"
                maxLength={6}
                value={verificationCode}
                onChange={(e) => setVerificationCode(e.target.value.replace(/\D/g, ''))}
                onKeyDown={(e) => e.key === 'Enter' && handleVerifyEmail()}
                className="text-center text-2xl font-mono tracking-widest"
              />
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                Code expires in 15 minutes
              </span>
              <Button
                variant="link"
                size="sm"
                onClick={handleResendCode}
                disabled={isResending || resendCooldown > 0}
                className="p-0 h-auto"
              >
                {isResending ? (
                  <>
                    <RefreshCw className="h-3 w-3 mr-1 animate-spin" />
                    Resending...
                  </>
                ) : resendCooldown > 0 ? (
                  `Resend in ${resendCooldown}s`
                ) : (
                  <>
                    <RefreshCw className="h-3 w-3 mr-1" />
                    Resend Code
                  </>
                )}
              </Button>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsVerifyDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleVerifyEmail} disabled={isVerifying}>
              {isVerifying ? "Verifying..." : "Verify"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
