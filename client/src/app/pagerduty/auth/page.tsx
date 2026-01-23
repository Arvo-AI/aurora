"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { pagerdutyService, PagerDutyStatus } from "@/lib/services/pagerduty";
import { PagerDutyConnectionStep } from "@/components/pagerduty/PagerDutyConnectionStep";
import { PagerDutyConnectedView } from "@/components/pagerduty/PagerDutyConnectedView";
import { PagerDutyWebhookStep } from "@/components/pagerduty/PagerDutyWebhookStep";
import { TokenInputModal } from "@/components/pagerduty/TokenInputModal";
import { ConnectionLoadingOverlay } from "@/components/ui/connection-loading-overlay";
import { isPagerDutyOAuthEnabled } from "@/lib/feature-flags";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

const CACHE_KEY = 'pagerduty_connection_status';

const getUserFriendlyError = (err: unknown): string => {
  if (!err) {
    return "An unexpected error occurred. Please try again.";
  }

  if (typeof err === 'string') {
    // Try to parse if it's a JSON string
    try {
      const parsed = JSON.parse(err);
      if (parsed.message) return parsed.message;
      if (parsed.error) return parsed.error;
    } catch {
      return err;
    }
  }

  if (err instanceof Error) {
    // Try to parse the message if it's JSON
    try {
      const parsed = JSON.parse(err.message);
      if (parsed.message) return parsed.message;
      if (parsed.error) return parsed.error;
    } catch {
      return err.message;
    }
  }

  if (typeof err === 'object') {
    const errorObj = err as Record<string, unknown>;
    // Prioritize 'message' field over 'error' field
    if (typeof errorObj.message === 'string') {
      return errorObj.message;
    }
    if (typeof errorObj.error === 'string') {
      return errorObj.error;
    }
  }

  return 'An unexpected error occurred. Please try again.';
};

export default function PagerDutyAuthPage() {
  const { toast } = useToast();
  const [displayName, setDisplayName] = useState("PagerDuty");
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<PagerDutyStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [showRotateModal, setShowRotateModal] = useState(false);
  const [showDisconnectDialog, setShowDisconnectDialog] = useState(false);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;

    if (connected) {
      localStorage.setItem('isPagerDutyConnected', 'true');
    } else {
      localStorage.removeItem('isPagerDutyConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const fetchAndUpdateStatus = async () => {
    const result = await pagerdutyService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      localStorage.setItem(CACHE_KEY, JSON.stringify(result));
    }

    updateLocalStorageConnection(result?.connected || false);
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== 'undefined') {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus) as PagerDutyStatus;
          setStatus(parsedStatus);
        }
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error('[pagerduty] Failed to load status', error);
      toast({
        title: 'Error',
        description: 'Unable to load PagerDuty status',
        variant: 'destructive',
      });
    }
  };

  useEffect(() => {
    loadStatus();
    
    // Listen for messages from OAuth popup
    const handleMessage = (event: MessageEvent) => {
      // Verify origin for security
      if (event.origin !== window.location.origin) {
        return;
      }
      
      if (event.data?.type === 'pagerduty-oauth-success') {
        toast({
          title: 'Success',
          description: 'PagerDuty connected successfully via OAuth.',
        });
        // Refresh status
        loadStatus(true);
        setLoading(false);
      } else if (event.data?.type === 'pagerduty-oauth-error') {
        const errorMessage = event.data?.error 
          ? `OAuth failed: ${event.data.error}` 
          : 'OAuth authentication failed';
        toast({
          title: 'Connection Failed',
          description: errorMessage,
          variant: 'destructive',
        });
        setLoading(false);
      }
    };
    
    window.addEventListener('message', handleMessage);
    
    return () => {
      window.removeEventListener('message', handleMessage);
    };
  }, []);

  const handleOAuthConnect = async () => {
    setLoading(true);
    setError(null);

    try {
      const { oauth_url } = await pagerdutyService.oauthLogin();
      
      // Open OAuth URL in popup window
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;
      
      const popup = window.open(
        oauth_url,
        'pagerduty-oauth',
        `width=${width},height=${height},left=${left},top=${top}`
      );

      if (!popup) {
        toast({
          title: 'Popup Blocked',
          description: 'Please allow popups for this site to use OAuth authentication.',
          variant: 'destructive',
        });
        setLoading(false);
        return;
      }

      // Poll for popup closure
      const pollTimer = setInterval(() => {
        if (popup.closed) {
          clearInterval(pollTimer);
          setLoading(false);
          // Refresh status after popup closes
          loadStatus(true);
        }
      }, 500);

    } catch (error: unknown) {
      console.error('[pagerduty] OAuth initiation failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'OAuth Failed',
        description: message,
        variant: 'destructive',
      });
      setLoading(false);
    }
  };

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);
    setIsConnecting(true);
    setError(null);
    setConnectionError(null);

    try {
      const result = await pagerdutyService.connect(token, displayName || "PagerDuty");
      setStatus(result);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        localStorage.setItem('isPagerDutyConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'PagerDuty connected successfully.',
      });

      updateLocalStorageConnection(true);

      // Update provider preferences
      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'pagerduty' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['pagerduty'] } }));
      } catch (prefErr: unknown) {
        console.warn('[pagerduty] Failed to update provider preferences', prefErr);
      }

      // Wait a bit before closing to show 100% completion
      setTimeout(() => {
        setIsConnecting(false);
      }, 500);
    } catch (error: unknown) {
      console.error('[pagerduty] Connect failed', error);
      const message = getUserFriendlyError(error);
      setConnectionError(message);
      // Don't close the overlay, keep it open to show the error
    } finally {
      setLoading(false);
      setToken('');
    }
  };

  const handleChangeToken = async (newToken: string) => {
    const result = await pagerdutyService.changeToken(newToken);
    setStatus(result);

    if (typeof window !== 'undefined') {
      localStorage.setItem(CACHE_KEY, JSON.stringify(result));
    }

    toast({
      title: 'Success',
      description: 'PagerDuty token changed successfully.',
    });
  };

  const handleDisconnectConfirm = async () => {
    setLoading(true);

    try {
      await pagerdutyService.disconnect();

      setStatus({ connected: false });
      setDisplayName("PagerDuty");

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEY);
        localStorage.removeItem('isPagerDutyConnected');
      }

      updateLocalStorageConnection(false);

      toast({
        title: 'Success',
        description: 'PagerDuty disconnected successfully.',
      });

      // Update provider preferences
      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'pagerduty' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[pagerduty] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[pagerduty] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'Failed to disconnect PagerDuty',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
      setShowDisconnectDialog(false);
    }
  };

  const isConnected = Boolean(status?.connected);

  return (
    <div className="container mx-auto py-6 px-4 max-w-2xl">
      {/* Header with centered logo */}
      <div className="mb-6 flex flex-col items-center text-center">
        <img src="/pagerduty-ar21.svg" alt="PagerDuty" className="h-24 w-auto mb-4" />
        <p className="text-sm text-muted-foreground max-w-xl">
          Receive incident alerts and manage on-call schedules directly in Aurora.
        </p>
      </div>

      <div className={isConnecting && !isConnected ? 'pointer-events-none opacity-50' : ''}>
        {!isConnected ? (
          <PagerDutyConnectionStep
            displayName={displayName}
            setDisplayName={setDisplayName}
            token={token}
            setToken={setToken}
            loading={loading}
            error={null}
            onConnect={handleConnect}
            onOAuthConnect={isPagerDutyOAuthEnabled() ? handleOAuthConnect : undefined}
          />
        ) : status ? (
          <div className="space-y-4">
            <PagerDutyConnectedView
              status={status}
              onChangeToken={() => setShowRotateModal(true)}
              onDisconnect={() => setShowDisconnectDialog(true)}
              loading={loading}
            />
            <PagerDutyWebhookStep />
          </div>
        ) : null}
      </div>

      {/* Loading Progress Indicator - Overlay (only during connection) */}
      <ConnectionLoadingOverlay
        isVisible={isConnecting}
        error={connectionError}
        onRetry={() => {
          setIsConnecting(false);
          setConnectionError(null);
        }}
        message="Validating connection..."
        successColor="#25c151"
      />

      <TokenInputModal
        open={showRotateModal}
        onOpenChange={setShowRotateModal}
        onSubmit={handleChangeToken}
        title="Change PagerDuty Token"
        description="Enter your new PagerDuty API token to replace the existing one."
        submitLabel="Change Token"
      />

      <AlertDialog open={showDisconnectDialog} onOpenChange={setShowDisconnectDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect PagerDuty?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove your PagerDuty credentials from Aurora. You can reconnect at any time.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDisconnectConfirm} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              Disconnect
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

