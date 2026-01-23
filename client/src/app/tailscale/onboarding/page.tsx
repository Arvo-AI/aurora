"use client";

import { useState } from "react";
import Image from "next/image";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, ExternalLink, CheckCircle2, AlertCircle, Info } from "lucide-react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { providerPreferencesService } from '@/lib/services/providerPreferences';

export default function TailscaleOnboardingPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [tailnet, setTailnet] = useState("");
  const router = useRouter();
  const { toast } = useToast();

  const handleConnect = async () => {
    if (!clientId || !clientSecret) {
      setError("OAuth Client ID and Client Secret are required");
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      // Use the API route which handles auth internally
      const response = await fetch('/api/tailscale/connect', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          clientId,
          clientSecret,
          tailnet: tailnet || undefined,
        }),
        credentials: 'include',
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to connect to Tailscale');
      }

      // Success - update localStorage
      localStorage.setItem("isTailscaleConnected", "true");

      // Auto-select Tailscale provider when connection succeeds
      await providerPreferencesService.smartAutoSelect('tailscale', true);

      // Dispatch event to notify provider status hooks
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      window.dispatchEvent(new CustomEvent('providerConnectionAction'));

      toast({
        title: "Tailscale Connected",
        description: `Successfully connected to ${data.tailnetName || data.tailnet || 'your tailnet'}. Found ${data.deviceCount || 0} device(s).`,
      });

      // Small delay to ensure toast is visible before redirect
      setTimeout(() => {
        router.push('/tailscale/ssh-setup');
      }, 500);
    } catch (err: unknown) {
      console.error('Tailscale connect error:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to connect to Tailscale';
      setError(errorMessage);
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-2xl mx-auto">
        <div className="text-center mb-8">
          <Image
            src="/tailscale.svg"
            alt="Tailscale Logo"
            width={64}
            height={64}
            className="mx-auto mb-4"
          />
          <h1 className="text-3xl font-bold text-foreground">Connect Your Tailscale Account</h1>
          <p className="mt-2 text-muted-foreground">
            Connect to Tailscale using OAuth client credentials for secure access to your tailnet.
          </p>
        </div>

        {/* OAuth Credentials Form */}
        <div className="bg-card shadow rounded-lg p-6 mb-6">
          <h2 className="text-xl font-semibold mb-4 text-foreground">Enter Your OAuth Credentials</h2>
          <p className="text-sm text-muted-foreground mb-4">
            Create an OAuth client in the Tailscale admin console under Settings &rarr; OAuth clients.
          </p>

          <div className="space-y-4">
            <div>
              <Label htmlFor="clientId">OAuth Client ID *</Label>
              <Input
                id="clientId"
                type="text"
                placeholder="k..."
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                className="mt-1"
              />
            </div>

            <div>
              <Label htmlFor="clientSecret">OAuth Client Secret *</Label>
              <Input
                id="clientSecret"
                type="password"
                placeholder="tskey-client-..."
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                className="mt-1"
              />
            </div>

            <div>
              <Label htmlFor="tailnet">Tailnet Name (optional)</Label>
              <Input
                id="tailnet"
                type="text"
                placeholder="example.com or - for default"
                value={tailnet}
                onChange={(e) => setTailnet(e.target.value)}
                className="mt-1"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Leave empty to use your default tailnet
              </p>
            </div>
          </div>
        </div>

        {/* Recommended Scopes */}
        <div className="bg-blue-500/10 border border-blue-500/20 rounded-lg p-4 mb-6 flex items-start gap-3">
          <Info className="h-5 w-5 text-blue-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-blue-500">Recommended OAuth Scopes</p>
            <p className="text-xs text-blue-400 mt-1">
              devices, keys, dns, acl, routes (read and write as needed)
            </p>
          </div>
        </div>

        {/* Error Display */}
        {error && (
          <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 mb-6 flex items-start gap-3">
            <AlertCircle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        {/* Connect Button */}
        <Button
          onClick={handleConnect}
          disabled={isLoading || !clientId || !clientSecret}
          className="w-full"
          size="lg"
        >
          {isLoading ? (
            <>
              <Loader2 className="mr-2 h-5 w-5 animate-spin" />
              Connecting...
            </>
          ) : (
            <>
              <CheckCircle2 className="mr-2 h-5 w-5" />
              Connect to Tailscale
            </>
          )}
        </Button>

        {/* Help Section */}
        <div className="mt-8 bg-muted/50 rounded-lg p-6">
          <h3 className="text-lg font-semibold mb-3 text-foreground">How to create an OAuth client</h3>
          <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
            <li>Go to the Tailscale Admin Console</li>
            <li>Navigate to Settings &rarr; OAuth clients</li>
            <li>Click &quot;Generate OAuth client&quot;</li>
            <li>Enter a description (e.g., &quot;Aurora Integration&quot;)</li>
            <li>Select the required scopes:
              <ul className="list-disc list-inside ml-4 mt-1">
                <li><code className="text-xs bg-muted px-1 rounded">devices:read</code> - List and view devices</li>
                <li><code className="text-xs bg-muted px-1 rounded">devices:write</code> - Authorize/remove devices</li>
                <li><code className="text-xs bg-muted px-1 rounded">keys:read</code> - List auth keys</li>
                <li><code className="text-xs bg-muted px-1 rounded">keys:write</code> - Create auth keys</li>
                <li><code className="text-xs bg-muted px-1 rounded">dns:read</code> - View DNS settings</li>
                <li><code className="text-xs bg-muted px-1 rounded">acl:read</code> - View ACL policy</li>
              </ul>
            </li>
            <li>Click &quot;Generate client&quot;</li>
            <li>Copy the Client ID and Client Secret (secret is only shown once!)</li>
          </ol>
          <a
            href="https://login.tailscale.com/admin/settings/oauth"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center mt-4 text-primary hover:underline"
          >
            Open Tailscale Admin Console
            <ExternalLink className="ml-1 h-4 w-4" />
          </a>
        </div>

        {/* Back Button */}
        <div className="mt-6 text-center">
          <Button variant="ghost" onClick={() => router.push('/chat')}>
            &larr; Back to Chat
          </Button>
        </div>
      </div>
    </div>
  );
}
