"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink, AlertCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { providerPreferencesService } from '@/lib/services/providerPreferences';
import { getEnv } from '@/lib/env';

const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');

export default function ScalewayOnboardingPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [organizationId, setOrganizationId] = useState("");
  const [projectId, setProjectId] = useState("");
  const router = useRouter();
  const { toast } = useToast();

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    
    if (!accessKey || !secretKey) {
      setError("Access Key and Secret Key are required");
      return;
    }

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
          description: "Please wait for authentication to complete before connecting to Scaleway.",
          variant: "destructive",
        });
        setIsLoading(false);
        return;
      }

      // Connect to Scaleway
      const response = await fetch(`${backendUrl}/scaleway_api/scaleway/connect`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        body: JSON.stringify({
          accessKey,
          secretKey,
          organizationId: organizationId || undefined,
          projectId: projectId || undefined,
        }),
        credentials: 'include',
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to connect to Scaleway');
      }

      // Success - update localStorage and redirect
      localStorage.setItem("isScalewayConnected", "true");
      localStorage.setItem("aurora_graph_discovery_trigger", "1");
      
      // Auto-select Scaleway provider when connection succeeds (legitimate connection)
      await providerPreferencesService.smartAutoSelect('scaleway', true);
      
      // Dispatch event to notify provider status hooks (same-tab updates)
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      window.dispatchEvent(new CustomEvent('providerConnectionAction'));
            
      toast({
        title: "Scaleway Connected",
        description: `Successfully connected. Found ${data.projectsCount || 0} project(s).`,
      });

      // Reset loading state before navigation in case it delays or fails
      setIsLoading(false);

      // Redirect to vm-config if user has VMs, otherwise to chat
      if (data.has_vms && data.redirect_to === '/vm-config') {
        router.push('/vm-config?provider=scaleway&connected=true');
      } else {
        // Redirect to chat (remove provider query param since auto-select handles it)
        router.push('/chat');
      }
    } catch (err: unknown) {
      console.error('Scaleway connect error:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to connect to Scaleway';
      setError(errorMessage);
      setIsLoading(false);
    }
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Scaleway Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect to Scaleway using your API credentials for secure access to your cloud resources.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Connect Your Scaleway Account</CardTitle>
          <CardDescription>Create an API key in Scaleway Console and connect it to Aurora</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-3 text-sm">
            <p className="text-muted-foreground">
              To connect Scaleway, you need to generate an API key from your Scaleway Console account.
            </p>
            
            <div className="space-y-2">
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">1.</span>
                <p>Go to the <a href="https://console.scaleway.com" target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Scaleway Console</a></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">2.</span>
                <p>Click on your profile → <strong>IAM &amp; API keys</strong></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">3.</span>
                <p>Click the <strong>&quot;API keys&quot;</strong> tab</p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">4.</span>
                <p>Click <strong>&quot;Generate API key&quot;</strong></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">5.</span>
                <p>Select <strong>&quot;Myself (IAM user)&quot;</strong> as the bearer</p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">6.</span>
                <p>Set expiration (1 year recommended)</p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">7.</span>
                <p>Copy the Access Key and Secret Key</p>
              </div>
            </div>

            <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
              <a
                href="https://console.scaleway.com/iam/api-keys"
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
              >
                Open Scaleway Console
                <ExternalLink className="w-3 h-3" />
              </a>
            </div>
          </div>

          {error && (
            <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          <form onSubmit={handleConnect} className="space-y-4">
            <div className="grid gap-2">
              <Label htmlFor="accessKey">Access Key *</Label>
              <Input
                id="accessKey"
                type="text"
                placeholder="SCWXXXXXXXXXXXXXXXXX"
                value={accessKey}
                onChange={(e) => setAccessKey(e.target.value)}
                required
                disabled={isLoading}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="secretKey">Secret Key *</Label>
              <Input
                id="secretKey"
                type="password"
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                required
                disabled={isLoading}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="organizationId">Organization ID (Optional)</Label>
              <Input
                id="organizationId"
                type="text"
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                value={organizationId}
                onChange={(e) => setOrganizationId(e.target.value)}
                disabled={isLoading}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="projectId">Default Project ID (Optional)</Label>
              <Input
                id="projectId"
                type="text"
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                disabled={isLoading}
              />
            </div>

            <div className="flex items-center justify-end pt-4">
              <Button type="submit" disabled={isLoading || !accessKey.trim() || !secretKey.trim()}>
                {isLoading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Connecting...
                  </>
                ) : (
                  "Connect Scaleway"
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <div className="mt-6 text-center">
        <Button variant="ghost" onClick={() => router.push('/chat')}>
          ← Back to Chat
        </Button>
      </div>
    </div>
  );
}
