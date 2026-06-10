'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, LogOut, RefreshCw, AlertTriangle } from 'lucide-react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';
import BitbucketWorkspaceBrowser from '@/components/bitbucket-workspace-browser';
import { isBitbucketOAuthEnabled } from '@/lib/feature-flags';

// Re-export service so existing imports from this path keep working
export { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';

const REQUIRED_API_TOKEN_SCOPES = [
  'read:user:bitbucket',
  'read:workspace:bitbucket',
  'read:project:bitbucket',
  'read:repository:bitbucket',
  'write:repository:bitbucket',
  'read:pullrequest:bitbucket',
  'write:pullrequest:bitbucket',
  'read:issue:bitbucket',
  'write:issue:bitbucket',
  'read:pipeline:bitbucket',
  'write:pipeline:bitbucket',
] as const;

export default function BitbucketProviderIntegration() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [displayName, setDisplayName] = useState<string>('');
  const [authType, setAuthType] = useState<string>('');
  const [missingScopes, setMissingScopes] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const { toast } = useToast();
  const oauthAvailable = isBitbucketOAuthEnabled();

  // API token form
  const [email, setEmail] = useState('');
  const [apiToken, setApiToken] = useState('');

  useEffect(() => {
    checkStatus();
  }, []);

  const checkStatus = async () => {
    try {
      const data = await BitbucketIntegrationService.checkStatus();
      setIsAuthenticated(data.connected || false);
      setDisplayName(data.display_name || data.username || '');
      setAuthType(data.auth_type || '');
      setMissingScopes(data.missing_scopes || []);
    } catch (error) {
      console.error('Error checking Bitbucket status:', error);
      setIsAuthenticated(false);
    } finally {
      setIsCheckingStatus(false);
    }
  };

  const handleOAuthLogin = async () => {
    setIsLoading(true);
    try {
      const oauthUrl = await BitbucketIntegrationService.initiateOAuth();
      const popup = window.open(oauthUrl, 'bitbucket-oauth', 'width=600,height=700,scrollbars=yes,resizable=yes');

      if (!popup) {
        toast({ title: "Popup Blocked", description: "Please allow popups for this site and try again", variant: "destructive" });
        setIsLoading(false);
        return;
      }

      const checkClosed = setInterval(() => {
        if (popup.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => {
            checkStatus();
            window.dispatchEvent(new CustomEvent('providerStateChanged'));
          }, 1000);
        }
      }, 1000);
    } catch (error: any) {
      console.error('Bitbucket OAuth error:', error);
      toast({ title: "Connection Failed", description: error.message || "Failed to connect to Bitbucket", variant: "destructive" });
      setIsLoading(false);
    }
  };

  const handleApiTokenLogin = async () => {
    if (!email || !apiToken) {
      toast({ title: "Error", description: "Email and API token are required", variant: "destructive" });
      return;
    }
    setIsLoading(true);
    setLoginError(null);
    try {
      const result = await BitbucketIntegrationService.connectWithApiToken(email, apiToken);
      setMissingScopes(result.missing_scopes || []);
      toast({ title: "Connected", description: "Bitbucket connected with API token" });
      setEmail('');
      setApiToken('');
      checkStatus();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    } catch (error: any) {
      console.error('API token login error:', error);
      setLoginError(error.message || "Failed to connect with API token");
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    try {
      await BitbucketIntegrationService.disconnect();
      setIsAuthenticated(false);
      setDisplayName('');
      setAuthType('');
      setMissingScopes([]);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Disconnected", description: "Bitbucket account disconnected successfully" });
    } catch (error: any) {
      console.error('Disconnect error:', error);
      toast({ title: "Error", description: error.message || "Failed to disconnect", variant: "destructive" });
    }
  };

  if (isCheckingStatus) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-3 border border-border rounded-lg">
        <Loader2 className="w-4 h-4 animate-spin" />
        Checking Bitbucket connection...
      </div>
    );
  }

  const apiTokenForm = (
    <>
      <p className="text-sm text-muted-foreground">
        Connect using a Bitbucket API token. Go to{' '}
        <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer" className="underline">
          Atlassian API tokens
        </a>
        , click &quot;Create API token with scopes&quot;, and grant these scopes:
      </p>
      <div className="text-xs bg-muted rounded-md p-2.5 space-y-0.5 font-mono">
        {REQUIRED_API_TOKEN_SCOPES.map((scope) => (
          <div key={scope}>{scope}</div>
        ))}
      </div>
      <Input
        type="email"
        placeholder="Bitbucket email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
      />
      <Input
        type="password"
        placeholder="API token"
        value={apiToken}
        onChange={(e) => setApiToken(e.target.value)}
      />
      <Button onClick={handleApiTokenLogin} disabled={isLoading || !email || !apiToken} className="w-full">
        {isLoading ? (
          <>
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            Connecting...
          </>
        ) : (
          "Connect"
        )}
      </Button>
      {loginError && (
        <p className="text-sm text-destructive">{loginError}</p>
      )}
    </>
  );

  return (
    <div className="space-y-4">
      {!isAuthenticated ? (
        oauthAvailable ? (
        <Tabs defaultValue="api-token" className="w-full">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="api-token">API Token</TabsTrigger>
            <TabsTrigger value="oauth">OAuth</TabsTrigger>
          </TabsList>
          <TabsContent value="oauth" className="space-y-3 mt-3">
            <p className="text-sm text-muted-foreground">
              Connect your Bitbucket Cloud account using OAuth. Create an{' '}
              <a href="https://support.atlassian.com/bitbucket-cloud/docs/use-oauth-on-bitbucket-cloud/" target="_blank" rel="noopener noreferrer" className="underline">
                OAuth consumer
              </a>{' '}
              in your workspace settings with these permissions:
            </p>
            <div className="text-xs bg-muted rounded-md p-2.5 space-y-0.5">
              <div>Account: <span className="font-medium">Read</span></div>
              <div>Workspace membership: <span className="font-medium">Read</span></div>
              <div>Projects: <span className="font-medium">Read</span></div>
              <div>Repositories: <span className="font-medium">Write</span></div>
              <div>Pull requests: <span className="font-medium">Write</span></div>
              <div>Issues: <span className="font-medium">Write</span></div>
              <div>Pipelines: <span className="font-medium">Write</span></div>
            </div>
            <Button onClick={handleOAuthLogin} disabled={isLoading} className="w-full">
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Connecting...
                </>
              ) : (
                "Connect with Bitbucket"
              )}
            </Button>
          </TabsContent>
          <TabsContent value="api-token" className="space-y-3 mt-3">
            {apiTokenForm}
          </TabsContent>
        </Tabs>
        ) : (
          <div className="space-y-3">{apiTokenForm}</div>
        )
      ) : (
        <div className="flex items-center justify-between p-3 border border-border rounded-lg">
          <div className="flex items-center gap-3">
            <img src="/bitbucket.svg" alt="Bitbucket" className="h-6 w-6 object-contain" />
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm">{displayName || 'Bitbucket'}</span>
                {authType && (
                  <Badge variant="outline" className="text-xs">{authType}</Badge>
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5">
                <Check className="w-3 h-3 text-green-500" />
                <span className="text-xs text-muted-foreground">Connected</span>
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                checkStatus();
                window.dispatchEvent(new CustomEvent('bitbucketRefresh'));
              }}
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-red-600 hover:text-red-700 hover:bg-red-50"
              onClick={handleDisconnect}
              title="Disconnect Bitbucket"
            >
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {isAuthenticated && missingScopes.length > 0 && (
        <div className="flex gap-2 p-3 border border-yellow-300 bg-yellow-50 dark:border-yellow-700 dark:bg-yellow-950 rounded-lg">
          <AlertTriangle className="h-4 w-4 text-yellow-600 dark:text-yellow-500 shrink-0 mt-0.5" />
          <div className="text-xs">
            <p className="font-medium text-yellow-800 dark:text-yellow-400">Limited permissions</p>
            <p className="text-yellow-700 dark:text-yellow-500 mt-0.5">
              Missing: {missingScopes.join(', ')}.{' '}
              <a
                href="https://id.atlassian.com/manage-profile/security/api-tokens"
                target="_blank"
                rel="noopener noreferrer"
                className="underline font-medium hover:text-yellow-900 dark:hover:text-yellow-300"
              >
                Recreate your token
              </a>{' '}
              with the required scopes to fix this.
            </p>
          </div>
        </div>
      )}

      {isAuthenticated && (
        <BitbucketWorkspaceBrowser />
      )}
    </div>
  );
}
