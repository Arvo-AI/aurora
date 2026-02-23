"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { getEnv } from '@/lib/env';
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";
import { Bell, Plus, Trash2, Check, Clock, RefreshCw, Mail } from "lucide-react";
import { useAuth, useUser } from "@/hooks/useAuthHooks";
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
  });
  const [savingPreferences, setSavingPreferences] = useState<Record<string, boolean>>({});
  const [isLoadingNotificationPref, setIsLoadingNotificationPref] = useState(true);
  
  const [primaryEmail, setPrimaryEmail] = useState<string>("");
  const [additionalEmails, setAdditionalEmails] = useState<RCAEmail[]>([]);
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
  const { userId } = useAuth();
  const { user } = useUser();

  // Load notification preferences
  useEffect(() => {
    const loadNotificationPreferences = async () => {
      if (!userId) return;
      
      try {
        const keys = ['rca_email_notifications', 'rca_email_start_notifications'];
        const newPreferences = { ...preferences };

        await Promise.all(keys.map(async (key) => {
          const response = await fetch(
            `${getEnv('NEXT_PUBLIC_BACKEND_URL')}/api/user-preferences?key=${key}`,
            {
              headers: {
                'Content-Type': 'application/json',
                'X-User-ID': userId,
              },
            }
          );

          if (response.ok) {
            const data = await response.json();
            if (data.value !== null && data.value !== undefined) {
              const value = typeof data.value === 'boolean' ? data.value : data.value === 'true' || data.value === true;
              // @ts-ignore
              newPreferences[key] = value;
            }
          }
        }));

        setPreferences(newPreferences);
      } catch (error) {
        console.error("Error loading notification preferences:", error);
      } finally {
        setIsLoadingNotificationPref(false);
      }
    };

    loadNotificationPreferences();
  }, [userId]);

  // Load email list
  useEffect(() => {
    loadEmails();
  }, [userId]);

  // Set primary email from authenticated user
  useEffect(() => {
    if (user?.emailAddresses?.[0]?.emailAddress) {
      setPrimaryEmail(user.emailAddresses[0].emailAddress);
    }
  }, [user]);

  // Resend cooldown timer
  useEffect(() => {
    if (resendCooldown > 0) {
      const timer = setTimeout(() => setResendCooldown(resendCooldown - 1), 1000);
      return () => clearTimeout(timer);
    }
  }, [resendCooldown]);

  const loadEmails = async () => {
    if (!userId) return;
    
    try {
      setIsLoadingEmails(true);
      const data = await listRCAEmails(userId);
      setPrimaryEmail(data.primary_email || "");
      setAdditionalEmails(data.additional_emails);
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
  };

  const handlePreferenceChange = async (key: string, enabled: boolean, label: string) => {
    if (!userId) return;
    
    // Optimistic update for smoother UI
    setPreferences(prev => ({ ...prev, [key]: enabled }));
    setSavingPreferences(prev => ({ ...prev, [key]: true }));

    try {
      const response = await fetch(
        `${getEnv('NEXT_PUBLIC_BACKEND_URL')}/api/user-preferences`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-ID': userId,
          },
          body: JSON.stringify({
            key,
            value: enabled,
          }),
        }
      );

      if (!response.ok) {
        throw new Error('Failed to save preference');
      }
    } catch (error) {
      console.error(`Error saving ${key} preference:`, error);
      // Revert on error
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
      toast({
        title: "Error",
        description: "Please enter an email address",
        variant: "destructive",
      });
      return;
    }

    // Simple email validation
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(newEmail)) {
      toast({
        title: "Error",
        description: "Please enter a valid email address",
        variant: "destructive",
      });
      return;
    }

    try {
      setIsAddingEmail(true);
      await addRCAEmail(userId, newEmail.trim().toLowerCase());
      
      toast({
        title: "Verification Code Sent",
        description: `A verification code has been sent to ${newEmail}`,
        variant: "default",
      });
      
      // Close add dialog and open verify dialog
      setIsAddDialogOpen(false);
      setVerifyingEmail(newEmail.trim().toLowerCase());
      setIsVerifyDialogOpen(true);
      setNewEmail("");
      
      // Reload email list
      await loadEmails();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Failed to add email";
      toast({
        title: "Error",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setIsAddingEmail(false);
    }
  };

  const handleVerifyEmail = async () => {
    if (!userId) return;
    if (!verificationCode.trim() || verificationCode.trim().length !== 6) {
      toast({
        title: "Error",
        description: "Please enter a valid 6-digit code",
        variant: "destructive",
      });
      return;
    }

    try {
      setIsVerifying(true);
      await verifyRCAEmail(userId, verifyingEmail, verificationCode.trim());
      
      toast({
        title: "Email Verified",
        description: "Your email has been verified successfully",
        variant: "default",
      });
      
      setIsVerifyDialogOpen(false);
      setVerificationCode("");
      setVerifyingEmail("");
      
      // Reload email list
      await loadEmails();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Failed to verify email";
      toast({
        title: "Error",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setIsVerifying(false);
    }
  };

  const handleResendCode = async () => {
    if (!userId) return;
    if (resendCooldown > 0) return;

    try {
      setIsResending(true);
      await resendVerificationCode(userId, verifyingEmail);
      
      toast({
        title: "Code Resent",
        description: "A new verification code has been sent",
        variant: "default",
      });
      
      setResendCooldown(60);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Failed to resend code";
      toast({
        title: "Error",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setIsResending(false);
    }
  };

  const handleToggleEmail = async (emailId: number, currentStatus: boolean, emailAddress: string) => {
    if (!userId) return;

    try {
      await toggleRCAEmail(userId, emailId, !currentStatus);
      
      toast({
        title: !currentStatus ? "Email Enabled" : "Email Disabled",
        description: `${emailAddress} will ${!currentStatus ? 'now' : 'no longer'} receive RCA notifications`,
        variant: "default",
      });
      
      // Reload email list
      await loadEmails();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Failed to toggle email";
      toast({
        title: "Error",
        description: errorMessage,
        variant: "destructive",
      });
    }
  };

  const handleRemoveEmail = async (emailId: number, emailAddress: string) => {
    if (!userId) return;
    
    if (!confirm(`Remove ${emailAddress} from RCA notifications?`)) {
      return;
    }

    try {
      await removeRCAEmail(userId, emailId);
      
      toast({
        title: "Email Removed",
        description: "Email address has been removed",
        variant: "default",
      });
      
      // Reload email list
      await loadEmails();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Failed to remove email";
      toast({
        title: "Error",
        description: errorMessage,
        variant: "destructive",
      });
    }
  };

  const openVerifyDialog = (email: string) => {
    setVerifyingEmail(email);
    setVerificationCode("");
    setIsVerifyDialogOpen(true);
  };

  return (
    <div className="space-y-6 min-h-0">
      {/* RCA Notifications Card */}
      <Card>
        <CardHeader>
          <CardTitle>RCA Notifications</CardTitle>
          <CardDescription>
            Manage how you receive root cause analysis notifications
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Primary Email */}
          <div className="space-y-2">
            <Label htmlFor="primary-email" className="text-sm font-medium">
              Primary Email
            </Label>
            <Input
              id="primary-email"
              type="email"
              value={primaryEmail}
              disabled
              className="bg-muted"
            />
            <p className="text-xs text-muted-foreground">
              Your primary email from your account
            </p>
          </div>

          {/* Notification Toggles */}
          <NotificationToggle
            title="Email Notifications"
            description="Receive email notifications when Aurora completes root cause analysis investigations"
            icon={<Bell className="h-4 w-4" />}
            checked={preferences.rca_email_notifications}
            onChange={(checked) => handlePreferenceChange('rca_email_notifications', checked, 'RCA email notifications')}
            isLoading={isLoadingNotificationPref || savingPreferences.rca_email_notifications}
          />
          
          <NotificationToggle
            title="Investigation Start Email Notifications"
            description="Also receive an email when Aurora begins an investigation"
            icon={<Bell className="h-4 w-4" />}
            checked={preferences.rca_email_start_notifications}
            onChange={(checked) => handlePreferenceChange('rca_email_start_notifications', checked, 'RCA investigation start notifications')}
            isLoading={isLoadingNotificationPref || savingPreferences.rca_email_start_notifications}
            disabled={!preferences.rca_email_notifications}
          />

          {/* Divider */}
          <div className="border-t my-4" />

          {/* Additional Email Recipients */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h4 className="font-medium flex items-center gap-2">
                  <Mail className="h-4 w-4" />
                  Additional Email Recipients
                </h4>
                <p className="text-sm text-muted-foreground">
                  Add other email addresses to receive RCA notifications
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setIsAddDialogOpen(true)}
                className="flex items-center gap-2"
              >
                <Plus className="h-4 w-4" />
                Add Email
              </Button>
            </div>

            {/* Email List */}
            {isLoadingEmails ? (
              <div className="text-sm text-muted-foreground">Loading...</div>
            ) : additionalEmails.length === 0 ? (
              <div className="text-sm text-muted-foreground p-4 border rounded-lg text-center">
                No additional email addresses added
              </div>
            ) : (
              <div className="space-y-2">
                {additionalEmails.map((email) => (
                  <div
                    key={email.id}
                    className="flex items-center justify-between p-3 border rounded-lg"
                  >
                    <div className="flex items-center gap-3 flex-1">
                      <span className="text-sm">{email.email}</span>
                      {email.is_verified ? (
                        <Badge variant="default" className="bg-green-500 flex items-center gap-1">
                          <Check className="h-3 w-3" />
                          Verified
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          Pending
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {email.is_verified && (
                        <div className="flex items-center gap-2 px-2">
                          <Switch
                            checked={email.is_enabled}
                            onCheckedChange={() => handleToggleEmail(email.id, email.is_enabled, email.email)}
                          />
                        </div>
                      )}
                      {!email.is_verified && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => openVerifyDialog(email.email)}
                        >
                          Verify
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleRemoveEmail(email.id, email.email)}
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Add Email Dialog */}
      <Dialog open={isAddDialogOpen} onOpenChange={setIsAddDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Email Address</DialogTitle>
            <DialogDescription>
              Enter an email address to receive RCA notifications. We'll send a verification code to confirm.
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

