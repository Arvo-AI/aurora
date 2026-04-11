"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ChevronLeft, Loader2 } from "lucide-react";
import { ProjectCache } from "@/components/cloud-provider/projects/projectUtils";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";
import { GcpServiceAccountForm } from "@/components/connectors/GcpServiceAccountForm";

export default function GcpAuthPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [oauthLoading, setOauthLoading] = useState(false);

  const handleOAuthConnect = async () => {
    setOauthLoading(true);
    try {
      const response = await fetch("/api/gcp/oauth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) {
        throw new Error(`Login request failed with status: ${response.status}`);
      }
      const data = await response.json();
      if (!data.login_url) {
        throw new Error("No OAuth URL received");
      }
      ProjectCache.invalidate("gcp");
      localStorage.setItem("aurora_graph_discovery_trigger", "1");
      window.location.href = data.login_url;
    } catch (error: unknown) {
      console.error("GCP OAuth connect failed", error);
      toast({
        title: "Failed to connect to Google Cloud",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
      setOauthLoading(false);
    }
  };

  const handleServiceAccountSuccess = () => {
    void fetchConnectedAccounts(true).catch(() => {});
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    }
    router.push("/connectors");
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <button
        onClick={() => router.push("/connectors")}
        className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors mb-6"
      >
        <ChevronLeft className="h-4 w-4" />
        Back to Connectors
      </button>

      <div className="flex items-center gap-4 mb-8">
        <div className="p-2 rounded-xl shadow-sm border overflow-hidden">
          <img src="/google-cloud-svgrepo-com.svg" alt="Google Cloud" className="h-9 w-9 object-contain rounded-md" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Google Cloud</h1>
          <p className="text-muted-foreground text-sm">
            Connect to Google Cloud Platform to manage cloud resources, monitor services, and deploy applications across your GCP infrastructure.
          </p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Connect Your Google Cloud Account</CardTitle>
          <CardDescription>
            Choose how Aurora should authenticate with your Google Cloud projects. You can always switch later by disconnecting and reconnecting.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="oauth" className="w-full">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="oauth">OAuth</TabsTrigger>
              <TabsTrigger value="service-account">Service Account</TabsTrigger>
            </TabsList>

            <TabsContent value="oauth" className="mt-6 space-y-4">
              <div className="space-y-2 text-sm text-muted-foreground">
                <p>
                  Sign in with your Google account. Aurora will use your OAuth
                  consent to access the projects you select. Best for individual
                  users and quick setup.
                </p>
                <p>You&apos;ll be redirected to Google to grant consent.</p>
              </div>
              <Button
                className="w-full"
                onClick={handleOAuthConnect}
                disabled={oauthLoading}
              >
                {oauthLoading ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Redirecting to Google...
                  </>
                ) : (
                  "Connect with Google"
                )}
              </Button>
            </TabsContent>

            <TabsContent value="service-account" className="mt-6">
              <GcpServiceAccountForm onSuccess={handleServiceAccountSuccess} />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
