"use client";

import { useState, useEffect } from "react";
import Image from "next/image";
import { Button } from "@/components/ui/button";
import { Loader2, ExternalLink, CheckCircle2 } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { getEnv } from '@/lib/env';

const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');

// OVH regions with their display names
const OVH_REGIONS = [
  { id: 'ovh-eu', name: 'Europe (EU)', description: 'OVHcloud Europe' },
  { id: 'ovh-ca', name: 'Canada (CA)', description: 'OVHcloud Canada' },
  { id: 'ovh-us', name: 'United States (US)', description: 'OVHcloud US' },
];

export default function OvhOnboardingPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [selectedRegion, setSelectedRegion] = useState('ovh-eu');
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const { toast } = useToast();

  // Handle error query params (redirected from backend on OAuth failure)
  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const errorParam = urlParams.get('error');

    if (errorParam) {
      // Map error codes to user-friendly messages
      const errorMessages: Record<string, string> = {
        'missing_params': 'Missing authorization parameters. Please try again.',
        'invalid_state': 'Session expired. Please try connecting again.',
        'config_error': 'OVH is not configured properly. Please contact support.',
        'invalid_grant': 'Authorization expired. Please try connecting again.',
        'token_exchange_failed': 'Failed to complete authentication. Please try again.',
        'no_access_token': 'No access token received. Please try again.',
        'timeout': 'Connection timed out. Please try again.',
        'network_error': 'Network error occurred. Please check your connection.',
        'callback_failed': 'Authentication failed. Please try again.',
      };
      setError(errorMessages[errorParam] || `OVH authentication failed: ${errorParam}`);
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, []);

  const handleConnect = async () => {
    setIsLoading(true);
    setError(null);

    try {
      // Get userId from API first
      let userId = "";
      try {
        const userResponse = await fetch("/api/getUserId");
        const userData = await userResponse.json();
        if (userData.userId) {
          userId = userData.userId;
        }
      } catch (error) {
        console.error("Error fetching user ID:", error);
      }

      if (!userId) {
        toast({
          title: "Authentication Required",
          description: "Please wait for authentication to complete before connecting to OVH.",
          variant: "destructive",
        });
        setIsLoading(false);
        return;
      }

      // Initiate OAuth flow
      const response = await fetch(`${backendUrl}/ovh_api/ovh/oauth2/initiate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        body: JSON.stringify({ endpoint: selectedRegion }),
        credentials: 'include',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || errorData.hint || 'Failed to initiate OVH authentication');
      }

      const data = await response.json();

      if (data.authorizationUrl) {
        // Signal graph discovery to trigger after OAuth completes
        localStorage.setItem("aurora_graph_discovery_trigger", "1");
        // Redirect to OVH authorization page
        window.location.href = data.authorizationUrl;
      } else {
        throw new Error('No authorization URL received from server');
      }
    } catch (err: any) {
      console.error('OVH connect error:', err);
      setError(err.message || 'Failed to connect to OVH');
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-2xl mx-auto">
        <div className="text-center mb-8">
          <Image
            src="/ovh.svg"
            alt="OVH Cloud Logo"
            width={64}
            height={64}
            className="mx-auto mb-4"
          />
          <h1 className="text-3xl font-bold text-foreground">Connect Your OVH Cloud Account</h1>
          <p className="mt-2 text-muted-foreground">
            Connect to OVH Cloud using OAuth2 for secure, seamless access to your cloud resources.
          </p>
        </div>

        {/* Region Selection */}
        <div className="bg-card shadow rounded-lg p-6 mb-6">
          <h2 className="text-xl font-semibold mb-4 text-foreground">Select Your Region</h2>
          <p className="text-sm text-muted-foreground mb-4">
            Choose the OVH Cloud region where your account is registered.
          </p>
          
          <Select value={selectedRegion} onValueChange={setSelectedRegion}>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Select a region" />
            </SelectTrigger>
            <SelectContent>
              {OVH_REGIONS.map((region) => (
                <SelectItem key={region.id} value={region.id}>
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{region.name}</span>
                    <span className="text-muted-foreground text-sm">- {region.description}</span>
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Connection Card */}
        <div className="bg-card shadow rounded-lg p-6 mb-6">
          <h2 className="text-xl font-semibold mb-4 text-foreground">OAuth2 Quick Connect</h2>
          
          <div className="space-y-4">
            <div className="flex items-start gap-3 p-4 bg-muted rounded-lg">
              <CheckCircle2 className="w-5 h-5 text-green-500 mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-medium text-foreground">Secure & Fast</p>
                <p className="text-sm text-muted-foreground">
                  Sign in directly with your OVH account. No credentials to copy or store.
                </p>
              </div>
            </div>

            <div className="flex items-start gap-3 p-4 bg-muted rounded-lg">
              <CheckCircle2 className="w-5 h-5 text-green-500 mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-medium text-foreground">Automatic Token Refresh</p>
                <p className="text-sm text-muted-foreground">
                  Your connection stays active with automatic token renewal.
                </p>
              </div>
            </div>

            {error && (
              <div className="p-4 bg-destructive/10 border border-destructive/20 rounded-lg">
                <p className="text-destructive text-sm">{error}</p>
              </div>
            )}

            <Button
              onClick={handleConnect}
              disabled={isLoading}
              className="w-full bg-[#000E9C] hover:bg-[#000E9C]/90 text-white"
              size="lg"
            >
              {isLoading ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Connecting to OVH Cloud...
                </>
              ) : (
                <>
                  <Image
                    src="/ovh.svg"
                    alt="OVH"
                    width={20}
                    height={20}
                    className="mr-2"
                  />
                  Connect with OVH Cloud
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Help Section */}
        <div className="bg-card shadow rounded-lg p-6">
          <h3 className="font-semibold mb-3 text-foreground">Need Help?</h3>
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>
              <a 
                href="https://help.ovhcloud.com/csm/en-manage-service-account"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline inline-flex items-center gap-1"
              >
                OVH Cloud Documentation
                <ExternalLink className="w-3 h-3" />
              </a>
            </p>
            <p>
              Make sure you have an active OVH Cloud account with Public Cloud projects enabled.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
