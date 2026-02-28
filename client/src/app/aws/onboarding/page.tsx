"use client";

import React, { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { 
  Loader2, 
  CheckCircle, 
  AlertCircle, 
  Copy,
  Cloud,
  Info,
  Download,
  Trash2,
  Upload
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { getEnv } from '@/lib/env';
import { useToast } from '@/hooks/use-toast';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

interface OnboardingData {
  workspaceId: string;
  externalId: string;
  status: 'not_started' | 'fully_configured';
  roleArn?: string;
  auroraAccountId?: string;
}

interface ConnectedAccount {
  account_id: string;
  role_arn: string;
  read_only_role_arn?: string;
  region?: string;
  connection_method?: string;
  last_verified_at?: string;
}

interface BulkResult {
  accountId: string;
  success: boolean;
  error?: string;
}

// Helper function to format AWS error messages for better UX
const formatAWSErrorMessage = (message: string): { title: string; description: string; isDetailed: boolean } => {
  if (message.includes('Aurora cannot assume this role') || 
      message.includes('Access denied when assuming role') ||
      message.includes('Cannot access AWS data') ||
      message.includes('Please verify:')) {
    
    const lines = message.split('\n');
    const title = lines[0];
    const description = lines.slice(1).join('\n').trim();
    
    return {
      title,
      description,
      isDetailed: true
    };
  }
  
  return {
    title: 'Configuration Error',
    description: message,
    isDetailed: false
  };
};

export default function AWSOnboardingPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [userId, setUserId] = useState<string | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [onboardingData, setOnboardingData] = useState<OnboardingData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isSettingRole, setIsSettingRole] = useState(false);
  const [copySuccess, setCopySuccess] = useState(false);
  const [policyCopySuccess, setPolicyCopySuccess] = useState(false);
  const [trustPolicyCopySuccess, setTrustPolicyCopySuccess] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [roleArn, setRoleArn] = useState('');
  const [isConfigured, setIsConfigured] = useState(false);
  const [credentialsConfigured, setCredentialsConfigured] = useState<boolean | null>(null);
  const [showDocs, setShowDocs] = useState(false);
  const [connectedAccounts, setConnectedAccounts] = useState<ConnectedAccount[]>([]);
  const [bulkInput, setBulkInput] = useState('');
  const [isBulkRegistering, setIsBulkRegistering] = useState(false);
  const [bulkResults, setBulkResults] = useState<BulkResult[] | null>(null);
  const [isDownloadingCfn, setIsDownloadingCfn] = useState(false);
  const [showBulkForm, setShowBulkForm] = useState(false);
  const [quickCreateUrl, setQuickCreateUrl] = useState<string | null>(null);
  const [stackSetsCommand, setStackSetsCommand] = useState<string | null>(null);
  const [stackSetsCopied, setStackSetsCopied] = useState(false);
  const [inactiveAccounts, setInactiveAccounts] = useState<ConnectedAccount[]>([]);
  const [reconnectingId, setReconnectingId] = useState<string | null>(null);

  // Auto-set connected flag when configured
  useEffect(() => {
    if (isConfigured) {
      localStorage.setItem('isAWSConnected', 'true');
      localStorage.setItem('cloudProvider', 'aws');
      localStorage.setItem('isAWSFetched', 'false');
    }
  }, [isConfigured]);

  // Check AWS credentials on component mount
  useEffect(() => {
    const checkCredentials = async () => {
      try {
        const response = await fetch(`${BACKEND_URL}/aws/env/check`, {
          method: 'GET',
          credentials: 'include',
        });
        if (response.ok) {
          const data = await response.json();
          setCredentialsConfigured(data.configured);
          if (!data.configured) {
            setShowDocs(true);
          }
        }
      } catch (err) {
        console.error("Failed to check AWS credentials:", err);
      }
    };
    checkCredentials();
  }, []);

  // Fetch user ID on component mount
  useEffect(() => {
    const fetchUserId = async () => {
      try {
        const response = await fetch("/api/getUserId");
        const data = await response.json();
        if (data.userId) {
          setUserId(data.userId);
        } else {
          setError("Unable to get user ID. Please log in again.");
        }
      } catch (err) {
        console.error("Failed to get userId:", err);
        setError("Failed to authenticate. Please try again.");
      }
    };
    fetchUserId();
  }, []);

  const fetchOnboardingData = useCallback(async () => {
    if (!userId) return;

    setIsLoading(true);
    setError(null);

    try {
      // First, get or create workspace for the user
      const workspaceResponse = await fetch(
        `${BACKEND_URL}/users/${userId}/workspaces`,
        {
          method: 'GET',
          credentials: 'include',
          headers: {
            'Content-Type': 'application/json',
            'X-User-ID': userId,
          },
        }
      );

      if (!workspaceResponse.ok) {
        const errorText = await workspaceResponse.text();
        throw new Error(`Failed to get workspace: ${workspaceResponse.status} - ${errorText}`);
      }

      const workspaceData = await workspaceResponse.json();
      let workspace = workspaceData.workspaces?.[0];
      
      // If no workspace exists, create one
      if (!workspace) {
        const createResponse = await fetch(
          `${BACKEND_URL}/users/${userId}/workspaces`,
          {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              'X-User-ID': userId,
            },
            body: JSON.stringify({ name: 'default' }),
          }
        );

        if (!createResponse.ok) {
          const errorText = await createResponse.text();
          throw new Error(`Failed to create workspace: ${createResponse.status} - ${errorText}`);
        }

        workspace = await createResponse.json();
      }

      setWorkspaceId(workspace.id);

      // Fetch onboarding info for this workspace
      const response = await fetch(
        `${BACKEND_URL}/workspaces/${workspace.id}/aws/links`,
        {
          method: 'GET',
          credentials: 'include',
          headers: {
            'Content-Type': 'application/json',
            'X-User-ID': userId,
          },
        }
      );

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Failed to fetch onboarding data: ${response.status} - ${errorText}`);
      }

      const data: OnboardingData = await response.json();
      setOnboardingData(data);

      // Check if already configured
      if (data.status === 'fully_configured' || data.roleArn) {
        setIsConfigured(true);
        setRoleArn(data.roleArn || '');
      }

    } catch (err) {
      console.error("Failed to fetch onboarding data:", err);
      setError("Failed to load onboarding data. Please try again.");
    } finally {
      setIsLoading(false);
    }
  }, [userId]);

  // Fetch onboarding data when userId is available
  useEffect(() => {
    if (userId) {
      fetchOnboardingData();
    }
  }, [userId, fetchOnboardingData]);

  const handleSetRole = async () => {
    if (!roleArn || !workspaceId || !userId) {
      setError("Please enter a role ARN");
      return;
    }

    setIsSettingRole(true);
    setError(null);

    try {
      const response = await fetch(
        `${BACKEND_URL}/workspaces/${workspaceId}/aws/role`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-ID': userId,
          },
          credentials: 'include',
          body: JSON.stringify({ roleArn }),
        }
      );

      if (!response.ok) {
        let errorMessage = `Failed to set role: ${response.status}`;
        try {
          const errorData = await response.json();
          if (errorData.message) {
            errorMessage = errorData.message;
          } else if (errorData.error) {
            errorMessage = errorData.error;
          }
        } catch (parseError) {
          console.error("Could not parse error response:", parseError);
        }
        throw new Error(errorMessage);
      }

      await fetchOnboardingData();
      setIsConfigured(true);
      localStorage.setItem("aurora_graph_discovery_trigger", "1");

    } catch (err) {
      console.error("Failed to set role:", err);
      setError(err instanceof Error ? err.message : "Failed to save role ARN. Please try again.");
    } finally {
      setIsSettingRole(false);
    }
  };

  const handleDisconnect = async () => {
    if (!workspaceId || !userId) return;
    const confirmed = window.confirm(
      'Disconnect all AWS accounts?\n\nThis removes Aurora\'s connections only. The IAM roles still exist in your AWS accounts. To fully revoke access, delete the CloudFormation stacks (or StackSet) in those accounts.'
    );
    if (!confirmed) return;
    setIsDisconnecting(true);
    try {
      const response = await fetch(
        `${BACKEND_URL}/workspaces/${workspaceId}/aws/cleanup`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-ID': userId,
          },
          credentials: 'include',
        }
      );
      if (!response.ok) throw new Error('Failed to disconnect AWS');
      // Reset local flags
      localStorage.removeItem('isAWSConnected');
      localStorage.removeItem('cloudProvider');
      localStorage.removeItem('isAWSFetched');
      // Notify other components to refresh (chat bar, connectors page, etc.)
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }
      // Refresh onboarding data
      await fetchOnboardingData();
      setIsConfigured(false);
      setRoleArn('');
    } catch (err) {
      console.error('Disconnect error:', err);
      setError('Failed to disconnect AWS.');
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleComplete = () => {
    router.push('/connectors');
  };

  const fetchConnectedAccounts = useCallback(async () => {
    if (!workspaceId || !userId) return;
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts`, {
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (res.ok) {
        const data = await res.json();
        setConnectedAccounts(data.accounts || []);
      }
    } catch (err) {
      console.error('Failed to fetch connected accounts:', err);
    }
  }, [workspaceId, userId]);

  const fetchQuickCreateData = useCallback(async () => {
    if (!workspaceId || !userId) return;
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/cfn-quickcreate`, {
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (res.ok) {
        const data = await res.json();
        setQuickCreateUrl(data.quickCreateUrl || null);
        setStackSetsCommand(data.stackSetsCommand || null);
      }
    } catch (err) {
      console.error('Failed to fetch quick-create data:', err);
    }
  }, [workspaceId, userId]);

  const fetchInactiveAccounts = useCallback(async () => {
    if (!workspaceId || !userId) return;
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts/inactive`, {
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (res.ok) {
        const data = await res.json();
        setInactiveAccounts(data.accounts || []);
      }
    } catch (err) {
      console.error('Failed to fetch inactive accounts:', err);
    }
  }, [workspaceId, userId]);

  useEffect(() => {
    if (workspaceId && userId) {
      fetchQuickCreateData();
      fetchInactiveAccounts();
      if (isConfigured) {
        fetchConnectedAccounts();
      }
    }
  }, [workspaceId, userId, isConfigured, fetchConnectedAccounts, fetchQuickCreateData, fetchInactiveAccounts]);

  const handleReconnect = async (accountId: string) => {
    if (!workspaceId || !userId) return;
    setReconnectingId(accountId);
    setError(null);
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts/${accountId}/reconnect`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-User-ID': userId },
      });
      if (!res.ok) {
        // Role is gone -- delete the stale entry and remove from the list
        await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts/${accountId}`, {
          method: 'DELETE',
          credentials: 'include',
          headers: { 'X-User-ID': userId },
        }).catch(() => {});
        setInactiveAccounts(prev => prev.filter(a => a.account_id !== accountId));
        toast({
          title: 'Role no longer exists',
          description: `The IAM role for account ${accountId} was deleted. Re-deploy it via the Quick-Create link.`,
          variant: 'destructive',
        });
        return;
      }
      setIsConfigured(true);
      localStorage.setItem('isAWSConnected', 'true');
      localStorage.setItem('cloudProvider', 'aws');
      await fetchConnectedAccounts();
      await fetchInactiveAccounts();
    } catch (err) {
      console.error('Reconnect error:', err);
      setError('Failed to reconnect. Check your network connection and try again.');
    } finally {
      setReconnectingId(null);
    }
  };

  const handleDownloadCfnTemplate = async () => {
    if (!workspaceId || !userId) return;
    setIsDownloadingCfn(true);
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/cfn-template?format=raw`, {
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (!res.ok) throw new Error('Failed to download template');
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'aurora-cross-account-role.yaml';
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      console.error('CFN download error:', err);
      setError('Failed to download CloudFormation template.');
    } finally {
      setIsDownloadingCfn(false);
    }
  };

  const handleBulkRegister = async () => {
    if (!workspaceId || !userId || !bulkInput.trim()) return;
    setIsBulkRegistering(true);
    setBulkResults(null);
    setError(null);

    try {
      const lines = bulkInput.trim().split('\n').filter(l => l.trim());
      const accounts = lines.map(line => {
        const parts = line.split(',').map(p => p.trim());
        const accountId = parts[0];
        const region = parts[1] || 'us-east-1';
        const roleName = parts[2] || 'AuroraReadOnlyRole';
        return {
          accountId,
          roleArn: `arn:aws:iam::${accountId}:role/${roleName}`,
          region,
        };
      });

      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts/bulk`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        body: JSON.stringify({ accounts }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `Bulk register failed: ${res.status}`);
      }

      const data = await res.json();
      setBulkResults(data.results || []);
      await fetchConnectedAccounts();

      if (data.succeeded > 0) {
        setIsConfigured(true);
        localStorage.setItem('isAWSConnected', 'true');
        localStorage.setItem('cloudProvider', 'aws');
      }
    } catch (err) {
      console.error('Bulk register error:', err);
      setError(err instanceof Error ? err.message : 'Bulk registration failed.');
    } finally {
      setIsBulkRegistering(false);
    }
  };

  const handleDeleteAccount = async (accountId: string) => {
    if (!workspaceId || !userId) return;
    const confirmed = window.confirm(
      `Disconnect account ${accountId}?\n\nThis removes Aurora's connection only. The IAM role still exists in your AWS account. To fully revoke access, delete the CloudFormation stack in that account.`
    );
    if (!confirmed) return;
    try {
      const res = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts/${accountId}`, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (!res.ok) throw new Error('Delete failed');
      const accountsRes = await fetch(`${BACKEND_URL}/workspaces/${workspaceId}/aws/accounts`, {
        credentials: 'include',
        headers: { 'X-User-ID': userId },
      });
      if (accountsRes.ok) {
        const data = await accountsRes.json();
        const remaining = data.accounts || [];
        setConnectedAccounts(remaining);
        if (remaining.length === 0) {
          setIsConfigured(false);
          localStorage.removeItem('isAWSConnected');
        }
      }
      await fetchInactiveAccounts();
    } catch (err) {
      console.error('Delete account error:', err);
      setError(`Failed to disconnect account ${accountId}.`);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopySuccess(true);
    setTimeout(() => setCopySuccess(false), 2000);
  };

  const copyPolicyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setPolicyCopySuccess(true);
    setTimeout(() => setPolicyCopySuccess(false), 2000);
  };

  const copyTrustPolicyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setTrustPolicyCopySuccess(true);
    setTimeout(() => setTrustPolicyCopySuccess(false), 2000);
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-12 h-12 animate-spin mx-auto mb-6 text-blue-400" />
          <p className="text-slate-300 text-lg">Loading AWS onboarding...</p>
        </div>
      </div>
    );
  }

  // Show documentation if credentials are not configured
  if (showDocs && credentialsConfigured === false) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center p-4 sm:p-6">
        <Card className="w-full max-w-2xl bg-black border-white/10 overflow-hidden">
          <CardHeader className="pb-4">
            <CardTitle className="text-white flex items-center space-x-2 text-lg sm:text-xl">
              <AlertCircle className="w-5 h-5 flex-shrink-0" />
              <span className="break-words">AWS Credentials Not Configured</span>
            </CardTitle>
            <CardDescription className="text-white/50 mt-2 text-sm">
              Aurora needs AWS credentials to connect to your AWS account. See the documentation for setup instructions.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 overflow-x-hidden">
            <div className="bg-white/5 rounded-lg p-4 border border-white/10 space-y-3 text-sm text-white/70">
              <div className="break-words">
                <p className="text-white/90 font-medium mb-1">1. Create an IAM user with this policy:</p>
                <div className="relative mt-2">
                  <pre className="bg-black/50 p-3 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre-wrap break-all">
{JSON.stringify({
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sts:AssumeRole"],
      "Resource": "*"
    }
  ]
}, null, 2)}
                  </pre>
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => copyPolicyToClipboard(JSON.stringify({
                      "Version": "2012-10-17",
                      "Statement": [
                        {
                          "Effect": "Allow",
                          "Action": ["sts:AssumeRole"],
                          "Resource": "*"
                        }
                      ]
                    }, null, 2))}
                    className="absolute top-2 right-2 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                  >
                    {policyCopySuccess ? <CheckCircle className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                  </Button>
                </div>
              </div>

              <div className="break-words">
                <p className="text-white/90 font-medium mb-1">2. Create access keys in AWS Console</p>
                <p className="text-white/60 text-xs">Copy the Access key ID and Secret access key</p>
              </div>

              <div className="break-words">
                <p className="text-white/90 font-medium mb-1">3. Add to <code className="bg-black/50 px-1 py-0.5 rounded text-xs">.env</code>:</p>
                <pre className="bg-black/50 p-3 rounded border border-white/10 text-xs mt-2 overflow-x-auto whitespace-pre break-all">{`AWS_ACCESS_KEY_ID=your-access-key-id
AWS_SECRET_ACCESS_KEY=your-secret-access-key
AWS_DEFAULT_REGION=us-east-1`}</pre>
              </div>

              <div className="break-words">
                <p className="text-white/90 font-medium mb-1">4. Rebuild and restart:</p>
                <pre className="bg-black/50 p-3 rounded border border-white/10 text-xs mt-2 overflow-x-auto whitespace-pre break-all">{`make down
make dev-build
make dev`}</pre>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <Button
                onClick={() => {
                  fetch(`${BACKEND_URL}/aws/env/check`, {
                    method: 'GET',
                    credentials: 'include',
                  })
                    .then(res => res.json())
                    .then(data => {
                      setCredentialsConfigured(data.configured);
                      if (data.configured) {
                        setShowDocs(false);
                        window.location.reload();
                      }
                    });
                }}
                className="flex-1 bg-white text-black hover:bg-white/90"
              >
                Check Again
              </Button>
              <Button
                variant="outline"
                onClick={() => router.push('/connectors')}
                className="border-white/10 hover:bg-white/5 text-white/70"
              >
                Back to Connectors
              </Button>
            </div>

            <div className="text-center pt-2 border-t border-white/10">
              <p className="text-xs text-white/40 break-words">
                Full documentation:{' '}
                <a
                  href="https://github.com/arvo-ai/aurora/blob/main/server/connectors/aws_connector/README.md"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-white/60 hover:text-white underline break-all"
                >
                  README
                </a>
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  // Show error page if credentials are configured but account ID cannot be retrieved (invalid credentials)
  if (onboardingData && !onboardingData.auroraAccountId && !isConfigured) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center p-4 sm:p-6">
        <Card className="w-full max-w-2xl bg-black border-white/10 overflow-hidden">
          <CardHeader className="pb-4">
            <CardTitle className="text-white flex items-center space-x-2 text-lg sm:text-xl">
              <AlertCircle className="w-5 h-5 flex-shrink-0 text-yellow-400" />
              <span className="break-words">AWS Credentials Issue</span>
            </CardTitle>
            <CardDescription className="text-white/50 mt-2 text-sm">
              Aurora detected your AWS credentials, but couldn't verify them with AWS.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 overflow-x-hidden">
            <Alert className="bg-yellow-500/10 border-yellow-500/20">
              <AlertCircle className="h-4 w-4 text-yellow-400" />
              <AlertDescription className="text-sm text-yellow-400">
                <strong>Configuration Issue:</strong> Your AWS credentials are set in the .env file, but Aurora couldn't retrieve your AWS account ID. This usually means the credentials are invalid, expired, or don't have the required permissions.
              </AlertDescription>
            </Alert>

            <div className="bg-white/5 rounded-lg p-4 border border-white/10 space-y-3 text-sm text-white/70">
              <p className="text-white/90 font-medium">Please verify:</p>
              <ul className="list-disc list-inside space-y-2 text-white/60 text-xs">
                <li>Your <code className="bg-black/50 px-1 py-0.5 rounded">AWS_ACCESS_KEY_ID</code> is correct</li>
                <li>Your <code className="bg-black/50 px-1 py-0.5 rounded">AWS_SECRET_ACCESS_KEY</code> is correct</li>
                <li>The credentials haven't expired or been rotated</li>
                <li>The IAM user has the <code className="bg-black/50 px-1 py-0.5 rounded">sts:AssumeRole</code> permission</li>
              </ul>

              <div className="pt-3 border-t border-white/10">
                <p className="text-white/90 font-medium mb-2">After fixing credentials:</p>
                <pre className="bg-black/50 p-3 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre break-all">{`make down
make dev-build
make dev`}</pre>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <Button
                onClick={() => window.location.reload()}
                className="flex-1 bg-white text-black hover:bg-white/90"
              >
                Check Again
              </Button>
              <Button
                variant="outline"
                onClick={() => router.push('/connectors')}
                className="border-white/10 hover:bg-white/5 text-white/70"
              >
                Back to Connectors
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error && !isConfigured) {
    const formattedError = formatAWSErrorMessage(error);
    return (
      <div className="min-h-screen bg-black flex items-center justify-center p-6">
        <Card className="w-full max-w-2xl bg-slate-900 border-slate-700">
          <CardHeader>
            <CardTitle className="text-red-400 flex items-center space-x-2">
              <AlertCircle className="w-5 h-5" />
              <span>{formattedError.title}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {formattedError.isDetailed ? (
              <div className="space-y-4">
                <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
                  <pre className="text-slate-300 text-sm whitespace-pre-wrap font-mono leading-relaxed">
                    {formattedError.description}
                  </pre>
                </div>
                <div className="text-slate-400 text-sm space-y-2">
                  <p>Need help? Check the AWS IAM console to verify your trust policy configuration.</p>
                  <Alert className="bg-blue-500/10 border-blue-500/20">
                    <Info className="h-4 w-4 text-blue-400" />
                    <AlertDescription className="text-xs text-blue-400">
                      <strong>Propagation Delay:</strong> If you just updated the trust policy, AWS changes can take up to <strong>5 minutes</strong> to propagate. Please wait a few minutes and try again.
                    </AlertDescription>
                  </Alert>
                </div>
              </div>
            ) : (
              <p className="text-slate-300 mb-6">{formattedError.description}</p>
            )}
            <Button 
              onClick={() => {
                setError(null);
                  fetchOnboardingData();
              }} 
              className="w-full bg-blue-600 hover:bg-blue-700 text-white mt-6"
            >
              Try Again
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!onboardingData) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center">
        <p className="text-slate-400">No onboarding data available.</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-black flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-4xl space-y-8">
        {/* Header */}
        <div className="text-center space-y-3">
          <h1 className="text-4xl font-bold text-white tracking-tight">AWS Security Onboarding</h1>
          <p className="text-white/50 max-w-xl mx-auto">
            Connect your AWS account using IAM role with STS AssumeRole.
          </p>
        </div>

        {/* Already Configured */}
        {isConfigured ? (
          <Card className="bg-black border-white/10">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                  <img src="/aws.ico" alt="AWS" className="h-12 w-12" />
                  <div>
                    <CardTitle className="flex items-center gap-3 text-white text-2xl">
                      <CheckCircle className="w-7 h-7 text-green-500" /> Setup Complete
                    </CardTitle>
                    <CardDescription className="text-white/50 mt-2">
                      {connectedAccounts.length > 1
                        ? `${connectedAccounts.length} AWS accounts connected`
                        : 'Your AWS account is now securely connected'}
                    </CardDescription>
                  </div>
                </div>
                <Button variant="ghost" onClick={handleDisconnect} disabled={isDisconnecting} className="text-white/50 hover:text-white hover:bg-white/5">
                  {isDisconnecting ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Disconnect All'}
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* Connected Accounts Table */}
              {connectedAccounts.length > 0 && (
                <div className="space-y-3">
                  <p className="text-sm font-medium text-white/70">Connected Accounts</p>
                  <div className="border border-white/10 rounded-lg overflow-hidden">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-white/5 text-white/50 text-xs">
                          <th className="text-left px-4 py-2">Account ID</th>
                          <th className="text-left px-4 py-2">Region</th>
                          <th className="text-left px-4 py-2">Role</th>
                          <th className="text-left px-4 py-2">Verified</th>
                          <th className="px-4 py-2"></th>
                        </tr>
                      </thead>
                      <tbody>
                        {connectedAccounts.map((acct) => (
                          <tr key={acct.account_id} className="border-t border-white/5 text-white/70">
                            <td className="px-4 py-2 font-mono text-xs">{acct.account_id}</td>
                            <td className="px-4 py-2 text-xs">{acct.region || 'us-east-1'}</td>
                            <td className="px-4 py-2 text-xs">
                              <a
                                href={`https://console.aws.amazon.com/iam/home?region=${acct.region || 'us-east-1'}#/roles/details/${acct.role_arn.split('/').pop()}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-blue-400 hover:text-blue-300 hover:underline font-mono"
                                title={acct.role_arn}
                              >
                                {acct.role_arn.split('/').pop()}
                              </a>
                            </td>
                            <td className="px-4 py-2 text-xs">{acct.last_verified_at ? new Date(acct.last_verified_at).toLocaleDateString() : '-'}</td>
                            <td className="px-4 py-2 text-right">
                              <Button variant="ghost" size="icon" onClick={() => handleDeleteAccount(acct.account_id)} className="h-7 w-7 text-white/30 hover:text-red-400 hover:bg-white/5">
                                <Trash2 className="w-3.5 h-3.5" />
                              </Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* Single account display fallback */}
              {connectedAccounts.length === 0 && onboardingData?.roleArn && (
                <div className="bg-white/5 border border-white/10 rounded-lg p-6 text-center space-y-2">
                  <p className="text-white/70">Aurora can now access your AWS account using secure STS AssumeRole</p>
                  <p className="text-white/50 text-sm font-mono mt-2">{onboardingData.roleArn}</p>
                </div>
              )}

              {/* Recently Disconnected -- Reconnect */}
              {inactiveAccounts.length > 0 && (
                <div className="space-y-3">
                  <p className="text-sm font-medium text-white/50">Recently Disconnected</p>
                  <p className="text-xs text-white/30">
                    The IAM roles still exist in these accounts. Reconnect below, or delete the CloudFormation stack in AWS to revoke access.
                  </p>
                  <div className="border border-white/5 rounded-lg overflow-hidden">
                    <table className="w-full text-sm">
                      <tbody>
                        {inactiveAccounts.map((acct) => (
                          <tr key={acct.account_id} className="border-t border-white/5 text-white/40">
                            <td className="px-4 py-2 font-mono text-xs">{acct.account_id}</td>
                            <td className="px-4 py-2 text-xs">{acct.region || 'us-east-1'}</td>
                            <td className="px-4 py-2 text-xs">
                              {acct.role_arn.split('/').pop()}
                            </td>
                            <td className="px-4 py-2 text-right">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => handleReconnect(acct.account_id)}
                                disabled={reconnectingId === acct.account_id}
                                className="border-white/10 hover:bg-white/5 text-white/60 text-xs h-7"
                              >
                                {reconnectingId === acct.account_id ? (
                                  <Loader2 className="w-3 h-3 animate-spin mr-1" />
                                ) : null}
                                Reconnect
                              </Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* Add More Accounts */}
              <div className="space-y-3 bg-white/5 border border-white/10 rounded-lg p-4">
                <p className="text-sm font-medium text-white/70">Add More Accounts</p>
                <p className="text-xs text-white/40">
                  Deploy the Aurora IAM role to another AWS account, then register it here.
                </p>

                {/* Primary: Quick-Create */}
                {quickCreateUrl && (
                  <a href={quickCreateUrl} target="_blank" rel="noopener noreferrer" className="block">
                    <Button variant="outline" size="sm" className="border-white/10 hover:bg-white/5 text-white/70 text-xs w-full justify-start">
                      <Cloud className="w-3 h-3 mr-1.5" />
                      Deploy role via AWS Console (Quick-Create)
                    </Button>
                  </a>
                )}

                <Button onClick={() => setShowBulkForm(!showBulkForm)} variant="outline" size="sm" className="border-white/10 hover:bg-white/5 text-white/70 text-xs w-full justify-start">
                  <Upload className="w-3 h-3 mr-1.5" />
                  {showBulkForm ? 'Hide Bulk Register' : 'Register accounts after deploying'}
                </Button>

                {/* Advanced options */}
                <details className="group">
                  <summary className="text-xs text-white/30 cursor-pointer hover:text-white/50 select-none">
                    More options: StackSets, CLI, download template
                  </summary>
                  <div className="mt-3 space-y-3 border-t border-white/5 pt-3">
                    {stackSetsCommand && (
                      <div className="space-y-1.5">
                        <p className="text-xs text-white/50">Deploy to all accounts via StackSets:</p>
                        <div className="relative">
                          <pre className="bg-black/50 p-3 pr-10 rounded border border-white/10 text-xs text-white/70 font-mono overflow-x-auto whitespace-pre">{stackSetsCommand}</pre>
                          <Button
                            variant="outline"
                            size="icon"
                            onClick={() => { navigator.clipboard.writeText(stackSetsCommand); setStackSetsCopied(true); setTimeout(() => setStackSetsCopied(false), 2000); }}
                            className="absolute top-2 right-2 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                          >
                            {stackSetsCopied ? <CheckCircle className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                          </Button>
                        </div>
                      </div>
                    )}
                    <Button onClick={handleDownloadCfnTemplate} disabled={isDownloadingCfn} variant="outline" size="sm" className="border-white/10 hover:bg-white/5 text-white/70 text-xs">
                      {isDownloadingCfn ? <Loader2 className="w-3 h-3 animate-spin mr-1.5" /> : <Download className="w-3 h-3 mr-1.5" />}
                      Download Template YAML
                    </Button>
                  </div>
                </details>
              </div>

              {/* Bulk Register Form */}
              {showBulkForm && (
                <div className="space-y-4 bg-white/5 border border-white/10 rounded-lg p-4">
                  <div className="space-y-2">
                    <p className="text-sm font-medium text-white/70">Bulk Register AWS Accounts</p>
                    <p className="text-xs text-white/40">
                      After deploying the CloudFormation template to your accounts, paste account IDs below.
                      One per line: <code className="bg-black/50 px-1 py-0.5 rounded">ACCOUNT_ID,REGION,ROLE_NAME</code> (region and role name are optional, defaults: us-east-1, AuroraReadOnlyRole)
                    </p>
                  </div>
                  <textarea
                    value={bulkInput}
                    onChange={(e) => setBulkInput(e.target.value)}
                    placeholder={"123456789012,us-east-1\n234567890123,eu-west-1\n345678901234"}
                    rows={6}
                    className="w-full bg-black/50 text-white border border-white/10 rounded-lg p-3 font-mono text-xs focus:outline-none focus:ring-1 focus:ring-white/20 placeholder:text-white/20"
                  />
                  <Button onClick={handleBulkRegister} disabled={isBulkRegistering || !bulkInput.trim()} className="bg-white text-black hover:bg-white/90">
                    {isBulkRegistering ? (
                      <><Loader2 className="w-4 h-4 animate-spin mr-2" /> Registering...</>
                    ) : (
                      <><Upload className="w-4 h-4 mr-2" /> Register Accounts</>
                    )}
                  </Button>

                  {/* Bulk Results */}
                  {bulkResults && (
                    <div className="space-y-2">
                      <p className="text-xs font-medium text-white/60">
                        Results: {bulkResults.filter(r => r.success).length} succeeded, {bulkResults.filter(r => !r.success).length} failed
                      </p>
                      <div className="max-h-40 overflow-y-auto space-y-1">
                        {bulkResults.map((r, i) => (
                          <div key={i} className={`text-xs px-3 py-1.5 rounded ${r.success ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                            <span className="font-mono">{r.accountId}</span>
                            {r.success ? ' - Connected' : ` - ${r.error}`}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              <Button onClick={handleComplete} className="w-full bg-white text-black hover:bg-white/90 h-11">
                Back to Connectors
              </Button>
            </CardContent>
          </Card>
        ) : (
          /* Manual Setup Form */
            <Card className="bg-black border-white/10">
              <CardHeader>
              <CardTitle className="text-white">Connect AWS Account</CardTitle>
              <CardDescription className="text-white/50">Deploy an IAM role to grant Aurora read-only access</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">

              {/* Primary: Quick-Create */}
              {quickCreateUrl ? (
                <div className="space-y-3">
                  <p className="text-sm text-white/70 font-medium">1. Deploy the IAM role</p>
                  <p className="text-xs text-white/40">
                    Click below to open the AWS Console with a pre-filled CloudFormation stack.
                    It creates a read-only IAM role that trusts Aurora.
                  </p>
                  <a href={quickCreateUrl} target="_blank" rel="noopener noreferrer" className="block">
                    <Button className="w-full bg-[#FF9900] text-black hover:bg-[#FF9900]/90 h-11 font-medium">
                      <Cloud className="w-4 h-4 mr-2" /> Open in AWS Console
                    </Button>
                  </a>
                  <p className="text-xs text-white/30 text-center">
                    Review the stack, check the IAM acknowledgement box, and click Create stack.
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-sm text-white/70 font-medium">1. Deploy the IAM role</p>
                  <p className="text-xs text-white/40">
                    Download the CloudFormation template and deploy it in your AWS account.
                  </p>
                  <Button onClick={handleDownloadCfnTemplate} disabled={isDownloadingCfn} className="w-full bg-[#FF9900] text-black hover:bg-[#FF9900]/90 h-11 font-medium">
                    {isDownloadingCfn ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Download className="w-4 h-4 mr-2" />}
                    Download CloudFormation Template
                  </Button>
                </div>
              )}

              {/* Step 2: Paste role ARN */}
              <div className="space-y-3">
                <p className="text-sm text-white/70 font-medium">2. Paste the Role ARN</p>
                <p className="text-xs text-white/40">
                  After the stack is created, copy the role ARN from the CloudFormation Outputs tab.
                </p>
                <Input
                  placeholder="arn:aws:iam::123456789012:role/AuroraReadOnlyRole"
                  value={roleArn}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setRoleArn(e.target.value)}
                  className="bg-white/5 text-white border-white/10 focus-visible:ring-white/20 font-mono text-sm"
                />
              </div>

              {/* Error Display */}
              {error && (
                <div className="space-y-3">
                  <Alert className="bg-red-500/10 border-red-500/20">
                    <AlertCircle className="h-4 w-4 text-red-400" />
                    <AlertDescription className="text-sm text-red-400">
                      {error}
                    </AlertDescription>
                  </Alert>
                  {(error.includes('cannot assume this role') || 
                    error.includes('Access denied') || 
                    error.includes('Role assumption failed')) && (
                    <Alert className="bg-blue-500/10 border-blue-500/20">
                      <Info className="h-4 w-4 text-blue-400" />
                      <AlertDescription className="text-xs text-blue-400">
                        <strong>Propagation Delay:</strong> If you just updated the trust policy, AWS changes can take up to <strong>5 minutes</strong> to propagate. Please wait a few minutes and try again.
                      </AlertDescription>
                    </Alert>
                  )}
                </div>
              )}

              {/* Connect Button */}
              <Button
                onClick={handleSetRole}
                disabled={!roleArn || isSettingRole}
                className="w-full bg-white text-black hover:bg-white/90 h-11"
              >
                {isSettingRole ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin mr-2" /> Connecting...
                  </>
                ) : (
                  <>
                    <Cloud className="w-4 h-4 mr-2" /> Connect AWS Account
                  </>
                )}
              </Button>

              {/* Advanced: Manual setup */}
              <details className="group">
                <summary className="text-xs text-white/30 cursor-pointer hover:text-white/50 select-none">
                  Advanced: manual role setup or StackSets
                </summary>
                <div className="mt-4 space-y-4 border-t border-white/5 pt-4">
                  {/* External ID */}
                  <div className="space-y-2">
                    <label className="text-xs text-white/50">External ID</label>
                    <div className="flex gap-2">
                      <Input
                        value={onboardingData.externalId}
                        readOnly
                        className="font-mono text-xs bg-white/5 text-white border-white/10 focus-visible:ring-white/20"
                      />
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={() => copyToClipboard(onboardingData.externalId)}
                        className="border-white/10 hover:bg-white/5 text-white/70"
                      >
                        {copySuccess ? <CheckCircle className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                      </Button>
                    </div>
                  </div>

                  {/* Trust policy */}
                  <div className="space-y-2">
                    <label className="text-xs text-white/50">Trust Policy JSON</label>
                    <div className="relative">
                      <pre className="text-white text-xs whitespace-pre-wrap font-mono bg-black/30 p-3 pr-10 rounded border border-white/10">
{JSON.stringify({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": `arn:aws:iam::${onboardingData.auroraAccountId}:root`
            },
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {
                    "sts:ExternalId": onboardingData.externalId
                }
            }
        }
    ]
}, null, 2)}
                      </pre>
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={() => copyTrustPolicyToClipboard(JSON.stringify({
                          "Version": "2012-10-17",
                          "Statement": [{
                            "Effect": "Allow",
                            "Principal": { "AWS": `arn:aws:iam::${onboardingData.auroraAccountId}:root` },
                            "Action": "sts:AssumeRole",
                            "Condition": { "StringEquals": { "sts:ExternalId": onboardingData.externalId } }
                          }]
                        }, null, 2))}
                        className="absolute top-2 right-2 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                      >
                        {trustPolicyCopySuccess ? <CheckCircle className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                      </Button>
                    </div>
                  </div>

                  {/* Download template */}
                  <Button onClick={handleDownloadCfnTemplate} disabled={isDownloadingCfn} variant="outline" size="sm" className="border-white/10 hover:bg-white/5 text-white/70 text-xs">
                    {isDownloadingCfn ? <Loader2 className="w-3 h-3 animate-spin mr-1.5" /> : <Download className="w-3 h-3 mr-1.5" />}
                    Download Template YAML
                  </Button>
                </div>
              </details>

            </CardContent>
          </Card>
        )}
        </div>
    </div>
  );
}
