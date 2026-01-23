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
  Info
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:5080';

interface OnboardingData {
  workspaceId: string;
  externalId: string;
  status: 'not_started' | 'fully_configured';
  roleArn?: string;
  auroraAccountId?: string;
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

    } catch (err) {
      console.error("Failed to set role:", err);
      setError(err instanceof Error ? err.message : "Failed to save role ARN. Please try again.");
    } finally {
      setIsSettingRole(false);
    }
  };

  const handleDisconnect = async () => {
    if (!workspaceId || !userId) return;
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
                    <CardDescription className="text-white/50 mt-2">Your AWS account is now securely connected</CardDescription>
                  </div>
                </div>
                <Button variant="ghost" onClick={handleDisconnect} disabled={isDisconnecting} className="text-white/50 hover:text-white hover:bg-white/5">
                  {isDisconnecting ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Disconnect'}
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="bg-white/5 border border-white/10 rounded-lg p-6 text-center space-y-2">
                <p className="text-white/70">Aurora can now access your AWS account using secure STS AssumeRole</p>
                {onboardingData.roleArn && (
                  <p className="text-white/50 text-sm font-mono mt-2">{onboardingData.roleArn}</p>
                )}
              </div>
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
              <CardDescription className="text-white/50">Create an IAM role and grant Aurora access</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
              {/* External ID Display */}
                <div className="space-y-3">
                <label className="text-sm text-white/70">External ID</label>
                  <div className="flex gap-2">
                    <Input
                      value={onboardingData.externalId}
                      readOnly
                      className="font-mono bg-white/5 text-white border-white/10 focus-visible:ring-white/20"
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
                <p className="text-xs text-white/40">Used to secure cross-account access. Include this in your IAM role trust policy.</p>
                </div>

              {/* Setup Instructions */}
              <div className="space-y-2 bg-white/5 border border-white/10 rounded-lg p-4">
                <p className="text-xs text-white/60 font-semibold">Setup Instructions:</p>

                <div className="space-y-3 mt-3">
                  <div>
                    <p className="text-xs text-white/60 mb-1">1. Create an IAM role in your AWS account with the permissions you want Aurora to have</p>
                  </div>

                  <div>
                    <p className="text-xs text-white/60 mb-1">2. Add this trust policy to the role:</p>
                    {!onboardingData.auroraAccountId ? (
                      <Alert className="bg-yellow-500/10 border-yellow-500/20 mt-2">
                        <AlertCircle className="h-4 w-4 text-yellow-400" />
                        <AlertDescription className="text-sm text-yellow-400">
                          Unable to detect Aurora's AWS account ID. Please ensure Aurora has AWS credentials configured. You can still create the role manually - the trust policy should allow the AWS account where Aurora is running to assume the role.
                        </AlertDescription>
                      </Alert>
                    ) : (
                      <div className="relative mt-2">
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
                          }, null, 2))}
                          className="absolute top-2 right-2 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                        >
                          {trustPolicyCopySuccess ? <CheckCircle className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                        </Button>
                      </div>
                    )}
                    <Alert className="bg-yellow-500/10 border-yellow-500/20 mt-2">
                      <AlertCircle className="h-4 w-4 text-yellow-400" />
                      <AlertDescription className="text-xs text-yellow-400">
                        <strong>Important:</strong> After updating the trust policy in AWS, changes can take up to <strong>5 minutes</strong> to propagate. If role assumption fails immediately after updating the trust policy, please wait a few minutes and try again.
                      </AlertDescription>
                    </Alert>
                  </div>

                  <div>
                    <p className="text-xs text-white/60 mb-1">3. Attach permission policies to the role (Aurora will inherit them when it assumes the role):</p>
                    <ul className="ml-4 space-y-1 text-xs text-white/40 mt-1">
                      <li>• Common choice: <span className="text-white/60">PowerUserAccess</span> (full access except IAM)</li>
                      <li>• Custom policies for your specific needs</li>
                    </ul>
                  </div>

                  <Alert className="bg-blue-500/10 border-blue-500/20 mt-3">
                    <Info className="h-4 w-4 text-blue-400" />
                    <AlertDescription className="text-xs text-blue-400">
                      Aurora automatically applies read-only restrictions in Ask mode to prevent accidental modifications
                    </AlertDescription>
                  </Alert>
                </div>
        </div>

              {/* Role ARN Input */}
              <div className="space-y-3">
                <label className="text-sm text-white/70">IAM Role ARN</label>
                <Input
                  placeholder="arn:aws:iam::123456789012:role/AuroraRole"
                  value={roleArn}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setRoleArn(e.target.value)}
                  className="bg-white/5 text-white border-white/10 focus-visible:ring-white/20 font-mono text-sm"
                />
                <p className="text-xs text-white/40">Enter the ARN of the IAM role you created above</p>
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
                        <strong>Propagation Delay:</strong> If you just updated the trust policy, AWS changes can take up to <strong>5 minutes</strong> to propagate. Please wait a few minutes and try again before troubleshooting further.
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
            </CardContent>
          </Card>
        )}
        </div>
    </div>
  );
}
