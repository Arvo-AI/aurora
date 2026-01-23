"use client";

import { useState, useEffect, useRef } from "react";
import Image from "next/image";
import { Button } from "@/components/ui/button";
import { Loader2, Copy, CheckCircle2, AlertCircle, Terminal, Key, ArrowLeft } from "lucide-react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";

interface SSHSetupData {
  sshPublicKey: string;
  instructions: string[];
  command: string;
  tailnet?: string;
}

export default function TailscaleSSHSetupPage() {
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sshData, setSSHData] = useState<SSHSetupData | null>(null);
  const [copied, setCopied] = useState<'key' | 'command' | null>(null);
  const router = useRouter();
  const { toast } = useToast();
  const copyTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    fetchSSHSetup();
    // Cleanup timeout on unmount
    return () => {
      if (copyTimeoutRef.current) {
        clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  const fetchSSHSetup = async () => {
    try {
      const response = await fetch('/api/tailscale/ssh-setup', {
        credentials: 'include',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to fetch SSH setup');
      }

      const data = await response.json();
      setSSHData(data);
    } catch (err) {
      console.error('SSH setup fetch error:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to load SSH setup';
      setError(errorMessage);
    } finally {
      setIsLoading(false);
    }
  };

  const copyToClipboard = async (text: string, type: 'key' | 'command') => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(type);
      toast({
        title: "Copied!",
        description: type === 'key' ? "SSH public key copied to clipboard" : "Command copied to clipboard",
      });
      // Clear any existing timeout before setting a new one
      if (copyTimeoutRef.current) {
        clearTimeout(copyTimeoutRef.current);
      }
      copyTimeoutRef.current = setTimeout(() => setCopied(null), 2000);
    } catch (err) {
      console.error('Copy failed:', err);
      toast({
        title: "Copy Failed",
        description: "Failed to copy to clipboard",
        variant: "destructive",
      });
    }
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto text-primary" />
          <p className="mt-4 text-muted-foreground">Loading SSH setup...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-background py-12 px-4 sm:px-6 lg:px-8">
        <div className="max-w-2xl mx-auto">
          <div className="text-center mb-8">
            <AlertCircle className="h-16 w-16 text-destructive mx-auto mb-4" />
            <h1 className="text-3xl font-bold text-foreground">SSH Setup Unavailable</h1>
            <p className="mt-2 text-muted-foreground">{error}</p>
          </div>

          <div className="bg-muted/50 rounded-lg p-6 text-center">
            <p className="text-sm text-muted-foreground mb-4">
              Make sure you have connected your Tailscale account first.
            </p>
            <Button onClick={() => router.push('/tailscale/onboarding')}>
              Connect Tailscale
            </Button>
          </div>

          <div className="mt-6 text-center">
            <Button variant="ghost" onClick={() => router.push('/chat')}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back to Chat
            </Button>
          </div>
        </div>
      </div>
    );
  }

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
          <h1 className="text-3xl font-bold text-foreground">SSH Setup</h1>
          <p className="mt-2 text-muted-foreground">
            Add Aurora&apos;s SSH public key to your devices to enable remote command execution.
          </p>
        </div>

        {/* SSH Public Key Section */}
        <div className="bg-card shadow rounded-lg p-6 mb-6">
          <div className="flex items-center gap-2 mb-4">
            <Key className="h-5 w-5 text-primary" />
            <h2 className="text-xl font-semibold text-foreground">Your SSH Public Key</h2>
          </div>
          <p className="text-sm text-muted-foreground mb-4">
            This is Aurora&apos;s unique SSH public key for your account. Add it to any device you want Aurora to access.
          </p>

          <div className="relative">
            <pre className="bg-muted rounded-lg p-4 text-xs overflow-x-auto whitespace-pre-wrap break-all font-mono text-foreground">
              {sshData?.sshPublicKey}
            </pre>
            <Button
              size="sm"
              variant="secondary"
              className="absolute top-2 right-2"
              onClick={() => copyToClipboard(sshData?.sshPublicKey || '', 'key')}
            >
              {copied === 'key' ? (
                <CheckCircle2 className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        {/* Quick Setup Command */}
        <div className="bg-card shadow rounded-lg p-6 mb-6">
          <div className="flex items-center gap-2 mb-4">
            <Terminal className="h-5 w-5 text-primary" />
            <h2 className="text-xl font-semibold text-foreground">Quick Setup Command</h2>
          </div>
          <p className="text-sm text-muted-foreground mb-4">
            Run this command on any device you want Aurora to access:
          </p>

          <div className="relative">
            <pre className="bg-muted rounded-lg p-4 text-xs overflow-x-auto whitespace-pre-wrap break-all font-mono text-foreground">
              {sshData?.command}
            </pre>
            <Button
              size="sm"
              variant="secondary"
              className="absolute top-2 right-2"
              onClick={() => copyToClipboard(sshData?.command || '', 'command')}
            >
              {copied === 'command' ? (
                <CheckCircle2 className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        {/* Step by Step Instructions */}
        <div className="bg-muted/50 rounded-lg p-6 mb-6">
          <h3 className="text-lg font-semibold mb-4 text-foreground">Setup Instructions</h3>
          <ol className="list-decimal list-inside space-y-3 text-sm text-muted-foreground">
            <li>
              <span className="font-medium text-foreground">SSH into your target device</span>
              <p className="ml-6 mt-1 text-xs">Connect to the device where you want Aurora to have access</p>
            </li>
            <li>
              <span className="font-medium text-foreground">Copy the command above</span>
              <p className="ml-6 mt-1 text-xs">Click the copy button to copy the setup command</p>
            </li>
            <li>
              <span className="font-medium text-foreground">Run the command on your device</span>
              <p className="ml-6 mt-1 text-xs">This adds Aurora&apos;s public key to your authorized_keys file</p>
            </li>
            <li>
              <span className="font-medium text-foreground">Repeat for each device</span>
              <p className="ml-6 mt-1 text-xs">Add the key to any device you want Aurora to access via SSH</p>
            </li>
          </ol>
        </div>

        {/* Security Note */}
        <div className="bg-yellow-500/10 border border-yellow-500/20 rounded-lg p-4 mb-6 flex items-start gap-3">
          <AlertCircle className="h-5 w-5 text-yellow-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-yellow-500">Security Note</p>
            <p className="text-xs text-yellow-400 mt-1">
              Only add this key to devices you trust Aurora to access. You can remove Aurora&apos;s access
              at any time by removing this key from ~/.ssh/authorized_keys on the device.
            </p>
          </div>
        </div>

        {/* Success indicator */}
        <div className="bg-green-500/10 border border-green-500/20 rounded-lg p-4 mb-6 flex items-start gap-3">
          <CheckCircle2 className="h-5 w-5 text-green-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-green-500">Ready to Use</p>
            <p className="text-xs text-green-400 mt-1">
              Once you&apos;ve added the key to a device, you can ask Aurora to run commands on it.
              For example: &quot;Run uptime on my-server&quot; or &quot;Check disk space on web-prod&quot;
            </p>
          </div>
        </div>

        {/* Back Button */}
        <div className="text-center">
          <Button variant="ghost" onClick={() => router.push('/chat')}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Back to Chat
          </Button>
        </div>
      </div>
    </div>
  );
}
