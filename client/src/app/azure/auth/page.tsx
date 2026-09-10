"use client";

import { useState, useEffect } from "react";
import Image from "next/image";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, Download, Check } from "lucide-react";
import { useRouter } from "next/navigation";
import { getEnv } from '@/lib/env';
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { copyToClipboard } from '@/lib/utils';

const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');


export default function AzureAuthPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [authMethod, setAuthMethod] = useState<'script_setup' | 'manual_credentials' | 'cloud_shell'>('cloud_shell');
  const [subscriptionId, setSubscriptionId] = useState("");
  const [subscription_name, setSubscriptionName] = useState("");
  const [subscriptionCount, setSubscriptionCount] = useState(0);
  const [credentials, setCredentials] = useState({
    tenantId: "",
    appId: "",
    password: "",
  });
  const [readOnlyJsonInput, setReadOnlyJsonInput] = useState("");
  const [readOnlyCredentials, setReadOnlyCredentials] = useState({
    tenantId: "",
    appId: "",
    password: "",
    subscriptionId: "",
  });
  const [showReadOnlyCredentialsPreview, setShowReadOnlyCredentialsPreview] = useState(false);
  const [readOnlyError, setReadOnlyError] = useState<string | null>(null);
  const [isReadOnlySectionExpanded, setIsReadOnlySectionExpanded] = useState(false);
  const [jsonInput, setJsonInput] = useState("");
  const [showCredentials, setShowCredentials] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [backendClusters, setBackendClusters] = useState<Array<{
    name: string;
    resourceGroup: string;
    subscriptionId: string;
  }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [copyButtonText, setCopyButtonText] = useState("Copy Command");

  const router = useRouter();
  const PROPAGATION_NOTE =
    " If you just generated these credentials in Cloud Shell, it can take 1–2 minutes for Azure permissions to propagate. Please wait a moment and try again.";

  const handleJsonPaste = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setJsonInput(value);
  };

  const handleReadOnlyJsonPaste = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setReadOnlyJsonInput(value);

    if (!value.trim()) {
      setReadOnlyCredentials({ tenantId: "", appId: "", password: "", subscriptionId: "" });
      setShowReadOnlyCredentialsPreview(false);
      setReadOnlyError(null);
      return;
    }

    try {
      const parsed = JSON.parse(value);
      const tenant = parsed.tenantId || parsed.tenant || "";
      const client = parsed.clientId || parsed.appId;
      const secret = parsed.clientSecret || parsed.password;
      const subId = parsed.subscriptionId || parsed.subscription_id || "";

      if (!client || !secret) {
        throw new Error("Missing required fields");
      }

      setReadOnlyCredentials({
        tenantId: tenant,
        appId: client,
        password: secret,
        subscriptionId: subId,
      });
      setShowReadOnlyCredentialsPreview(true);
      setReadOnlyError(null);
    } catch (err) {
      console.error("Invalid read-only JSON:", err);
      setReadOnlyError("Please enter valid read-only credentials with tenantId (optional), clientId, and clientSecret fields");
      setShowReadOnlyCredentialsPreview(false);
    }
  };

  const buildReadOnlyPayload = (fallbackTenantId: string, fallbackSubscriptionId: string) => {
    if (!readOnlyCredentials.appId || !readOnlyCredentials.password) {
      return undefined;
    }

    return {
      tenantId: readOnlyCredentials.tenantId || fallbackTenantId,
      clientId: readOnlyCredentials.appId,
      clientSecret: readOnlyCredentials.password,
      subscriptionId: readOnlyCredentials.subscriptionId || fallbackSubscriptionId,
    };
  };

  const handleSubmit = async (e?: React.FormEvent | React.MouseEvent) => {
    e?.preventDefault();
    setIsLoading(true);
    setError(null);
    
    // Set Azure as refreshing in localStorage to show loading spinner on other pages
    localStorage.setItem("isAzureConnecting", "true");
    localStorage.setItem("isAzureConnecting_timestamp", Date.now().toString());
    // Dispatch event to notify other components of state change
    window.dispatchEvent(new CustomEvent('providerStateChanged'));

    try {
      // Get userId from API
      let userId = "";
      try {
        const userResponse = await fetch("/api/getUserId");
        const userData = await userResponse.json();
        if (userData.userId) {
          userId = userData.userId;
        }
      } catch (error) {
        console.error("Error fetching user ID from API:", error);
      }

      let currentCredentials = credentials;

      // Parse JSON if provided
      if (jsonInput) {
        try {
          const parsed = JSON.parse(jsonInput);

          // Check if it's the new format with agent/readonly sections
          if (parsed.agent && parsed.readonly) {
            // New format from updated script
            const agentCreds = parsed.agent;
            const readOnlyCreds = parsed.readonly;

            if (!agentCreds.tenantId || !agentCreds.clientId || !agentCreds.clientSecret) {
              throw new Error("Missing required fields in agent credentials");
            }

            // Set agent credentials
            currentCredentials = {
              tenantId: agentCreds.tenantId,
              appId: agentCreds.clientId,
              password: agentCreds.clientSecret
            };
            setCredentials(currentCredentials);

            // Set subscription ID if provided
            if (agentCreds.subscriptionId) {
              setSubscriptionId(agentCreds.subscriptionId);
            }

            // Set read-only credentials if valid
            if (readOnlyCreds.clientId && readOnlyCreds.clientSecret) {
              setReadOnlyCredentials({
                tenantId: readOnlyCreds.tenantId || agentCreds.tenantId,
                appId: readOnlyCreds.clientId,
                password: readOnlyCreds.clientSecret,
                subscriptionId: readOnlyCreds.subscriptionId || agentCreds.subscriptionId
              });
              setShowReadOnlyCredentialsPreview(true);
            }

            setShowCredentials(true);
          } else if (parsed.tenantId && parsed.clientId && parsed.clientSecret) {
            // Old flat format (backward compatibility)
            currentCredentials = {
              tenantId: parsed.tenantId,
              appId: parsed.clientId,
              password: parsed.clientSecret
            };
            setCredentials(currentCredentials);

            // Set subscription ID if provided
            if (parsed.subscriptionId) {
              setSubscriptionId(parsed.subscriptionId);
            }

            setShowCredentials(true);
          } else {
            throw new Error("Missing required fields in JSON");
          }
        } catch (error: any) {
          console.error("Invalid JSON:", error);
          setError("Please enter valid JSON credentials. Supported formats: {agent: {...}, readonly: {...}} or {tenantId, clientId, clientSecret}");
          setIsLoading(false);
          return;
        }
      }

      // Validate credentials
      if (!currentCredentials.tenantId || !currentCredentials.appId || !currentCredentials.password) {
        setError("Please provide all required credentials (Tenant ID, Client ID, and Client Secret)");
        setIsLoading(false);
        return;
      }

      if (readOnlyJsonInput.trim() && !readOnlyCredentials.appId) {
        setError("Please fix the read-only credentials JSON before continuing");
        setIsLoading(false);
        return;
      }

      const readOnlyPayload = buildReadOnlyPayload(
        currentCredentials.tenantId,
        subscriptionId || readOnlyCredentials.subscriptionId || "",
      );

      const requestBody: Record<string, any> = {
        userId,
        tenantId: currentCredentials.tenantId,
        clientId: currentCredentials.appId,
        clientSecret: currentCredentials.password,
        subscriptionId: subscriptionId || "",
        subscriptionName: subscription_name || "",
        authMethod: "service_principal",
      };

      if (readOnlyPayload) {
        requestBody.readOnlyCredentials = readOnlyPayload;
      }

      // Make request to backend login
      const response = await fetch(`/api/proxy/azure/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json();
        console.error("Azure login response error:", errorData);
        throw new Error(errorData.error || "Failed to login to Azure");
      }

      // After successful login, fetch data
      const fetchResponse = await fetch(`/api/proxy/azure/fetch_data?userId=${encodeURIComponent(userId)}`, {
        method: "GET",
      });
      
      if (!fetchResponse.ok) {
        throw new Error("Failed to fetch Azure data");
      }

      const fetchData = await fetchResponse.json();

      
      // Set local storage flags
      localStorage.setItem("isAzureConnected", "true");
      localStorage.setItem("cloudProvider", "azure");
      localStorage.setItem("isAzureFetched", "true"); // Data already fetched during connection
      localStorage.setItem("isLoggedAurora", "true");
      localStorage.setItem("aurora_graph_discovery_trigger", "1");
      
      // Auto-select Azure provider when connection succeeds (legitimate connection)
      const { providerPreferencesService } = await import('@/lib/services/providerPreferences');
      await providerPreferencesService.smartAutoSelect('azure', true);
      
      // Clear connecting flag and dispatch event to notify other components of state change
      localStorage.removeItem("isAzureConnecting");
      localStorage.removeItem("isAzureConnecting_timestamp");
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      

      
      setIsConnected(true);
      setShowCredentials(true);
      
      // Redirect to chat after successful Azure authentication
      setTimeout(() => {
        router.replace('/chat');
      }, 200);
      


      // Update subscription info from response
      if (fetchData.subscription_id) {
        setSubscriptionId(fetchData.subscription_id);
      } else if (fetchData.subscriptionId) {
        setSubscriptionId(fetchData.subscriptionId);
      }
      
      if (fetchData.subscription_name) {
        setSubscriptionName(fetchData.subscription_name);
      } else if (fetchData.subscriptionName) {
        setSubscriptionName(fetchData.subscriptionName);
      }

      setSubscriptionCount(fetchData.subscription_count || 0);
      
      // Check if we have clusters data and set them
      if (fetchData.clusters && fetchData.clusters.length > 0) {
        setBackendClusters(fetchData.clusters);
      }

    } catch (error: any) {
      console.error("Error connecting to Azure:", error);
      const message = (typeof error?.message === 'string' ? error.message : 'Failed to connect to Azure').trim();
      setError(message.includes('permissions to propagate') ? message : `${message}${/[.!?]$/.test(message) ? '' : '.'}${PROPAGATION_NOTE}`);
      localStorage.setItem("isAzureConnected", "false");
      // Clear connecting flag on error
      localStorage.removeItem("isAzureConnecting");
    } finally {
      setIsLoading(false);
      // Ensure connecting flag is always cleared
      localStorage.removeItem("isAzureConnecting");
      // Dispatch event to notify state changes
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    }
  };

  const handleConnect = async () => {
    setIsLoading(true);
    setError(null);

    try {
      // Get userId from API
      let userId = "";
      try {
        const userResponse = await fetch("/api/getUserId");
        const userData = await userResponse.json();
        if (userData.userId) {
          userId = userData.userId;
        }
      } catch (error) {
        console.error("Error fetching user ID from API:", error);
      }

      if (readOnlyJsonInput.trim() && !readOnlyCredentials.appId) {
        setError("Please fix the read-only credentials JSON before continuing");
        setIsLoading(false);
        return;
      }

      const readOnlyPayload = buildReadOnlyPayload(
        credentials.tenantId,
        subscriptionId || readOnlyCredentials.subscriptionId || "",
      );

      const requestBody: Record<string, any> = {
        userId,
        tenantId: credentials.tenantId,
        clientId: credentials.appId,
        clientSecret: credentials.password,
        subscriptionId: subscriptionId,
        subscriptionName: subscription_name,
        authMethod: "service_principal",
      };

      if (readOnlyPayload) {
        requestBody.readOnlyCredentials = readOnlyPayload;
      }

      // Make request to backend login
      const response = await fetch(`/api/proxy/azure/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json();
        console.error("Azure login response error:", errorData);
        throw new Error(errorData.error || "Failed to login to Azure");
      }

      // After successful login, fetch data
      const fetchResponse = await fetch(`/api/proxy/azure/fetch_data?userId=${encodeURIComponent(userId)}`, {
        method: "GET",
      });

      if (!fetchResponse.ok) {
        throw new Error("Failed to fetch Azure data");
      }

      const fetchData = await fetchResponse.json();

      
      localStorage.setItem("isAzureConnected", "true");
      localStorage.setItem("cloudProvider", "azure");
      localStorage.setItem("isAzureFetched", "true"); // Data already fetched during connection
      localStorage.setItem("isLoggedAurora", "true");
      
      // Auto-select Azure provider when connection succeeds (legitimate connection)
      const { providerPreferencesService } = await import('@/lib/services/providerPreferences');
      await providerPreferencesService.smartAutoSelect('azure', true);
      
      // Clear connecting flag and dispatch event to notify other components of state change
      localStorage.removeItem("isAzureConnecting");
      localStorage.removeItem("isAzureConnecting_timestamp");
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      
      setIsConnected(true);

      setSubscriptionCount(fetchData.subscription_count || 0);

      if (fetchData.clusters && fetchData.clusters.length > 0) {
        setBackendClusters(fetchData.clusters);
      }
      
      // Redirect to chat after successful Azure authentication
      setTimeout(() => {
        router.replace('/chat');
      }, 200);

    } catch (error: any) {
      console.error("Error connecting to Azure:", error);
      const message = (typeof error?.message === 'string' ? error.message : 'Failed to connect to Azure').trim();
      setError(message.includes('permissions to propagate') ? message : `${message}${/[.!?]$/.test(message) ? '' : '.'}${PROPAGATION_NOTE}`);
      localStorage.setItem("isAzureConnected", "false");
      // Clear connecting flag on error
      localStorage.removeItem("isAzureConnecting");
      localStorage.removeItem("isAzureConnecting_timestamp");
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    } finally {
      setIsLoading(false);
      // Ensure connecting flag is always cleared
      localStorage.removeItem("isAzureConnecting");
      // Dispatch event to notify state changes
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    }
  };

  const downloadSetupScript = async () => {
    try {
      const response = await fetch(`/api/proxy/azure/setup-script`, {
        method: 'GET',
      });

      if (!response.ok) {
        throw new Error('Failed to download setup script');
      }

      const scriptContent = await response.text();
      const blob = new Blob([scriptContent], { type: 'text/plain' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.style.display = 'none';
      a.href = url;
      a.download = 'setup-aurora-access.sh';
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (error) {
      console.error('Error downloading setup script:', error);
      setError('Failed to download setup script. Please try again.');
    }
  };





  const renderActionButton = () => (
        <Button
          onClick={(e) => {
            console.log("BUTTON CLICKED - About to call handleSubmit");
            handleSubmit(e);
          }}
      disabled={isLoading || (!jsonInput && (!credentials.tenantId || !credentials.appId || !credentials.password))}
          className="w-full bg-blue-600 hover:bg-blue-700"
        >
          {isLoading ? (
            <div className="flex items-center justify-center">
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
          <span>Connecting to Azure...</span>
            </div>
          ) : (
            'Connect to Azure'
          )}
        </Button>
  );

  return (
    <ConnectorAuthGuard connectorName="Azure">
      <div className="min-h-screen bg-background py-12 px-4 sm:px-6 lg:px-8">
        <div className="max-w-4xl mx-auto">
          <div className="text-center mb-8">
            <Image
              src="/azure.ico"
              alt="Azure Logo"
              width={48}
              height={48}
              sizes="48px"
              className="mx-auto mb-4"
            />
          <h1 className="text-3xl font-bold text-foreground">Connect Your Azure Account</h1>
        </div>

        {/* Authentication Method Selection */}
        <div className="bg-card shadow rounded-lg p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4 text-foreground">Choose Setup Method</h2>
          <div className="space-y-4">
            <div className="flex items-center space-x-3">
              <input
                type="radio"
                id="script_setup"
                name="authMethod"
                value="script_setup"
                checked={authMethod === 'script_setup'}
                onChange={(e) => setAuthMethod(e.target.value as 'script_setup' | 'manual_credentials' | 'cloud_shell')}
                className="h-4 w-4 text-blue-600 dark:text-blue-400"
              />
              <label htmlFor="script_setup" className="flex-1 cursor-pointer">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-lg font-medium text-foreground">Manual Setup</h3>
                    <p className="text-muted-foreground text-sm">
                      Run our script to automatically create a service principal with read-only permissions 
                      for Aurora to access your Azure subscription.
                    </p>
                  </div>
                  <div className="bg-green-100 dark:bg-green-800 text-green-800 dark:text-green-200 text-xs px-2 py-1 rounded-full">
                    Easy
                  </div>
                </div>
              </label>
            </div>
            
            <div className="flex items-center space-x-3">
              <input
                type="radio"
                id="cloud_shell"
                name="authMethod"
                value="cloud_shell"
                checked={authMethod === 'cloud_shell'}
                onChange={(e) => setAuthMethod(e.target.value as 'script_setup' | 'manual_credentials' | 'cloud_shell')}
                className="h-4 w-4 text-blue-600 dark:text-blue-400"
              />
              <label htmlFor="cloud_shell" className="flex-1 cursor-pointer">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-lg font-medium text-foreground">Cloud Shell Setup</h3>
                    <p className="text-muted-foreground text-sm">
                      Use Azure Cloud Shell - no downloads or installations required. 
                      Opens directly in your browser with our script pre-loaded.
                    </p>
                  </div>
                  <div className="bg-gradient-to-r from-green-500 to-emerald-500 text-white text-xs font-semibold px-3 py-1 rounded-full shadow-sm">Easiest</div>
                </div>
              </label>
            </div>
            
            {/* Manual credentials option commented out - keeping automatic and browser options only */}
            {/* <div className="flex items-center space-x-3">
              <input
                type="radio"
                id="manual_credentials"
                name="authMethod"
                value="manual_credentials"
                checked={authMethod === 'manual_credentials'}
                onChange={(e) => setAuthMethod(e.target.value as 'script_setup' | 'manual_credentials' | 'cloud_shell')}
                className="h-4 w-4 text-blue-600 dark:text-blue-400"
              />
              <label htmlFor="manual_credentials" className="flex-1 cursor-pointer">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-lg font-medium text-foreground">Enter Credentials Manually</h3>
                    <p className="text-muted-foreground text-sm">
                      If you already have Aurora service principal credentials
                    </p>
                  </div>
                  <div className="bg-blue-100 dark:bg-blue-800 text-blue-800 dark:text-blue-200 text-xs px-2 py-1 rounded-full">
                    Advanced
                  </div>
                </div>
              </label>
            </div> */}
          </div>
        </div>

        {/* Manual Script Setup Flow */}
        {authMethod === 'script_setup' && (
          <div className="bg-card shadow rounded-lg p-6 mb-8">
            <h2 className="text-xl font-semibold mb-4 text-foreground">Manual Aurora Setup</h2>
            
              <div className="space-y-6">
                <div className="bg-muted border border-border rounded-md p-4">
                <h3 className="text-lg font-medium text-foreground mb-2">Complete Setup in One Script</h3>
                  <p className="text-muted-foreground text-sm">
                  Our setup script creates two service principals - one for agent mode, one read-only for ask mode - assigns built-in Azure roles to every subscription in scope, and detects AKS clusters. You&apos;ll get a JSON response to paste into Aurora.
                  </p>
                </div>

              <div className="bg-muted border border-border rounded-md p-4 mb-6">
                <div className="flex">
                  <div className="ml-3">
                    <h3 className="text-sm font-medium text-foreground">Prerequisites</h3>
                    <div className="text-sm text-muted-foreground mt-1">
                      <p>- Azure CLI installed (<a href="https://docs.microsoft.com/en-us/cli/azure/install-azure-cli" target="_blank" rel="noopener noreferrer" className="underline text-primary hover:text-primary/80">install guide</a>)</p>
                      <p>- Logged into Azure: <code className="bg-card border border-border px-2 py-1 rounded text-foreground">az login</code></p>
                      <p>- Owner or Contributor + User Access Administrator permissions</p>
                      <p>- Application Administrator role in Entra ID</p>
                    </div>
                  </div>
                </div>
                </div>

                <div className="space-y-6">
                <h3 className="text-lg font-medium text-foreground">Download Setup Script</h3>
                <p className="text-muted-foreground">
                  Run this in Azure Cloud Shell, which already has the Azure CLI, jq and kubectl available.
                </p>

                <div className="bg-muted rounded-lg p-4">
                  <h4 className="font-medium text-foreground mb-2">Azure Cloud Shell</h4>
                  <p className="text-sm text-muted-foreground mb-3">
                    Grants Aurora least-privilege access across every enabled subscription.
                  </p>
                  <Button
                    onClick={downloadSetupScript}
                    variant="outline"
                    className="flex items-center space-x-2 w-full"
                  >
                    <Download className="w-4 h-4" />
                    <span>Download Setup Script</span>
                  </Button>
                  <div className="mt-3 bg-muted rounded-md p-3">
                    <p className="text-xs text-muted-foreground mb-1">Upload it to Cloud Shell, then run:</p>
                    <code className="text-foreground text-xs">bash setup-aurora-access.sh</code>
                    <p className="text-xs text-muted-foreground mt-2 mb-1">To scope to a management group instead:</p>
                    <code className="text-foreground text-xs">bash setup-aurora-access.sh &lt;management-group-id&gt;</code>
                  </div>
                </div>
              </div>

                <div className="space-y-4">
                <h3 className="text-lg font-medium text-foreground">Step 2: Run the Script and Enter Credentials</h3>
                  <p className="text-muted-foreground">
                  After running the script, it will output service principal credentials. Copy the JSON output and paste it below:
                </p>
                
                <div className="mb-4">
                  <Label htmlFor="scriptJsonInput" className="text-foreground">Aurora Setup Results</Label>
                  <textarea
                    id="scriptJsonInput"
                    value={jsonInput}
                    onChange={handleJsonPaste}
                    className="w-full h-32 p-2 border border-border rounded-md font-mono text-sm bg-card text-foreground placeholder-muted-foreground"
                    placeholder={'{\n  "agent": { "tenantId": "...", "clientId": "...", "clientSecret": "..." },\n  "readonly": { "tenantId": "...", "clientId": "...", "clientSecret": "..." },\n  "subscriptions": ["...", "..."]\n}'}
                  />
                </div>

                {showCredentials && (
                  <div className="bg-green-50 dark:bg-green-900 border border-green-200 dark:border-green-700 rounded-md p-4 mb-4">
                    <h4 className="font-medium text-green-800 dark:text-green-200 mb-2">Credentials Detected:</h4>
                    <div className="space-y-2">
                    <div className="flex items-center">
                                        <span className="text-muted-foreground w-32">Tenant ID:</span>
                <span className="font-mono text-sm text-foreground">{credentials.tenantId}</span>
                    </div>
                      <div className="flex items-center">
                <span className="text-muted-foreground w-32">Client ID:</span>
                <span className="font-mono text-sm text-foreground">{credentials.appId}</span>
                      </div>
                      <div className="flex items-center">
                <span className="text-muted-foreground w-32">Client Secret:</span>
                <span className="font-mono text-sm text-foreground">{"*".repeat(20)}</span>
                      </div>
                    </div>
                  </div>
                )}

                {error && (
                  <div className="bg-red-50 dark:bg-red-900 border border-red-200 dark:border-red-700 rounded-md p-4">
                    <div className="flex">
                      <div className="flex-shrink-0">
                        <svg className="h-5 w-5 text-red-400 dark:text-red-300" viewBox="0 0 20 20" fill="currentColor">
                          <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                        </svg>
                      </div>
                      <div className="ml-3">
                        <h3 className="text-sm font-medium text-red-800 dark:text-red-200">Error</h3>
                        <p className="text-sm text-red-700 dark:text-red-300 mt-1">{error}</p>
                      </div>
                    </div>
                  </div>
                )}

                <div className="mt-4">
                  {renderActionButton()}
                      </div>
                </div>

                <div className="bg-muted rounded-lg p-4">
                <h4 className="font-medium text-foreground mb-2">What the script does:</h4>
                <ul className="text-sm text-muted-foreground space-y-1">
                  <li>- Creates a custom "Aurora Manager" role with comprehensive permissions</li>
                  <li>- Creates a service principal specifically for Aurora</li>
                  <li>- Detects existing AKS clusters and automatically assigns permissions</li>
                  <li>- Outputs service principal credentials in JSON format</li>
                  <li>- Includes instructions for revoking access later</li>
                </ul>
                  </div>
                </div>
              </div>
            )}

        {/* Cloud Shell Setup Flow */}
        {authMethod === 'cloud_shell' && (
          <div className="bg-card shadow rounded-lg p-6 mb-8">
            <h2 className="text-xl font-semibold mb-4 text-foreground">Azure Cloud Shell Setup</h2>
            
            <div className="space-y-6">
              <div className="bg-muted border border-border rounded-md p-4">
                <h3 className="text-lg font-medium text-foreground mb-2">Super Easy Setup</h3>
                <p className="text-muted-foreground text-sm">
                  Open Azure Cloud Shell and run our comprehensive setup script. 
                  No downloads, installations, or command-line knowledge required!
                </p>
              </div>

              <div className="bg-muted border border-border rounded-md p-4">
                <h3 className="text-lg font-medium text-foreground mb-2">What This Will Do</h3>
                <ul className="text-muted-foreground text-sm space-y-1">
                  <li>• Open Azure Cloud Shell directly in your browser</li>
                  <li>• Create a custom "Aurora Manager" role with comprehensive permissions</li>
                  <li>• Create a service principal with the custom role</li>
                  <li>• Automatically detect and configure AKS cluster permissions</li>
                  <li>• Generate JSON credentials ready to paste into Aurora</li>
                </ul>
              </div>

              <div className="space-y-4">
                <div className="flex flex-col sm:flex-row gap-3">
                  <Button
                     onClick={() => {
                       // Use GitHub Gist for localhost, deployed frontend proxy for production
                       const scriptUrl = backendUrl?.includes('localhost') 
                         ? 'https://gist.githubusercontent.com/isiddharthsingh/45f810e9c82af2855b5b394b84567f21/raw/34a1a32317c14ee2bf4667a82adde4b7b166b226/gistfile1.sh'
                         : `${backendUrl}/azure/setup-script`;
                       const command = `curl -s ${scriptUrl} | bash`;
                       copyToClipboard(command);
                       
                       setCopyButtonText(' Copied!');
                       setTimeout(() => {
                         setCopyButtonText('Copy Command');
                       }, 2000);
                     }}
                    variant="outline"
                    className="flex-1 border-2 border-blue-600 text-blue-600 hover:bg-blue-50 dark:border-blue-400 dark:text-blue-400 dark:hover:bg-blue-950 font-semibold py-3 px-4 rounded-lg transition-all duration-200"
                  >
                    <div className="flex items-center justify-center space-x-2">
                      <span>{copyButtonText}</span>
                    </div>
                  </Button>

                  <Button
                     onClick={() => {
                       // Use GitHub Gist for localhost, deployed frontend proxy for production
                       const scriptUrl = backendUrl?.includes('localhost') 
                         ? 'https://gist.githubusercontent.com/isiddharthsingh/45f810e9c82af2855b5b394b84567f21/raw/34a1a32317c14ee2bf4667a82adde4b7b166b226/gistfile1.sh'
                         : `${backendUrl}/azure/setup-script`;
                       const script = `curl -s ${scriptUrl} | bash`;
                        
                        const encodedScript = encodeURIComponent(script);
                        // Force interactive login for the account the user selects (avoid reusing existing SSO)
                        const shellUrl = `https://shell.azure.com/bash?command=${encodedScript}&prompt=login%20select_account`;
                        window.open(shellUrl, '_blank', 'width=1200,height=800');
                     }}
                    className="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-semibold py-3 px-4 rounded-lg transition-all duration-200 transform hover:scale-[1.02] shadow-lg"
                  >
                    <div className="flex items-center justify-center space-x-2">
                      <span>Open Cloud Shell</span>
                    </div>
                  </Button>
                </div>
                
                <div className="bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <div className="flex-shrink-0 mt-0.5">
                      <svg className="h-4 w-4 text-blue-500" fill="currentColor" viewBox="0 0 20 20">
                        <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
                      </svg>
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-300">
                      <strong>How to use:</strong> Click "Copy Command" then paste it into Cloud Shell and press Enter. The automated command will run the full setup script.
                    </div>
                  </div>
                </div>
              </div>

              <div className="border-t border-border pt-6">
                <h3 className="text-lg font-medium text-foreground mb-4">Step 2: Enter the Generated Credentials</h3>
                <p className="text-muted-foreground mb-4">
                  After the setup script completes in Cloud Shell, copy the JSON credentials and paste them here:
                </p>
                
                <div className="mb-4">
                  <Label htmlFor="cloudShellJsonInput" className="text-foreground">Aurora Setup Results</Label>
                  <textarea
                    id="cloudShellJsonInput"
                    value={jsonInput}
                    onChange={handleJsonPaste}
                    className="w-full h-32 p-2 border border-border rounded-md font-mono text-sm bg-card text-foreground placeholder-muted-foreground"
                    placeholder={'{\n  "agent": { "tenantId": "...", "clientId": "...", "clientSecret": "..." },\n  "readonly": { "tenantId": "...", "clientId": "...", "clientSecret": "..." },\n  "subscriptions": ["...", "..."]\n}'}
                  />
                </div>

                {showCredentials && (
                  <div className="bg-green-50 dark:bg-green-900 border border-green-200 dark:border-green-700 rounded-md p-4 mb-4">
                    <h4 className="font-medium text-green-800 dark:text-green-200 mb-2">Credentials Detected:</h4>
                    <div className="space-y-2">
                      <div className="flex items-center">
                        <span className="text-muted-foreground w-32">Tenant ID:</span>
                        <span className="font-mono text-sm text-foreground">{credentials.tenantId}</span>
                      </div>
                      <div className="flex items-center">
                        <span className="text-muted-foreground w-32">Client ID:</span>
                        <span className="font-mono text-sm text-foreground">{credentials.appId}</span>
                      </div>
                      <div className="flex items-center">
                        <span className="text-muted-foreground w-32">Client Secret:</span>
                        <span className="font-mono text-sm text-foreground">{"*".repeat(20)}</span>
                      </div>
                    </div>
                  </div>
                )}

                {error && (
                  <div className="bg-red-50 dark:bg-red-900 border border-red-200 dark:border-red-700 rounded-md p-4 mb-4">
                    <div className="flex">
                      <div className="flex-shrink-0">
                        <svg className="h-5 w-5 text-red-400 dark:text-red-300" viewBox="0 0 20 20" fill="currentColor">
                          <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                        </svg>
                      </div>
                      <div className="ml-3">
                        <h3 className="text-sm font-medium text-red-800 dark:text-red-200">Error</h3>
                        <p className="text-sm text-red-700 dark:text-red-300 mt-1">{error}</p>
                      </div>
                    </div>
                  </div>
                )}

                <div className="mt-4">
                  {renderActionButton()}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Manual Credentials Flow */}
        {/* Success Message */}
        {isConnected && (
          <div className="bg-green-50 dark:bg-green-900 border border-green-200 dark:border-green-700 rounded-md p-4">
            <div className="flex items-center">
              <Check className="h-5 w-5 text-green-400 dark:text-green-300" />
              <h3 className="ml-3 text-sm font-medium text-green-800 dark:text-green-200">Successfully Connected!</h3>
            </div>
            <p className="mt-2 text-sm text-green-700 dark:text-green-300">
              {subscriptionCount > 1
                ? <>Aurora is now connected to <strong>{subscriptionCount} Azure subscriptions</strong>. Aurora investigates across all of them.</>
                : <>Aurora is now connected to your Azure subscription.{subscription_name && <span> Subscription: <strong>{subscription_name}</strong></span>}</>}
            </p>
            <div className="mt-4">
              <Button
                onClick={() => {
                                  // Add small delay to ensure localStorage changes are fully processed
                setTimeout(() => {
                  if (window.history.length > 1) {
                    window.history.back();
                  } else {
                    window.location.href = '/chat';
                  }
                }, 200);
                }}
                className="bg-green-600 hover:bg-green-700 dark:bg-green-700 dark:hover:bg-green-600"
              >
                Continue
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
    </ConnectorAuthGuard>
  );
} 
