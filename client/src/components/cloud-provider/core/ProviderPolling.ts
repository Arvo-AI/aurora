import { Provider } from '../types';

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL;

export interface ProviderError {
  message: string;
  action?: 'open_console' | string;
}

export interface ProviderPollingConfig {
  onProvidersUpdate: (updater: (prev: Provider[]) => Provider[]) => void;
  onSetupProgressUpdate: (inProgress: boolean) => void;
  fetchProviderData: (providerId: string) => Promise<void>;
  autoSelectProvider: (providerId: string, isLegitimateConnection?: boolean) => Promise<void>;
  onError?: (error: ProviderError | Error | string, duration?: number) => void;
}

export class ProviderPolling {
  private gcpSetupInterval: NodeJS.Timeout | null = null;
  private cleanup: (() => void) | null = null;
  private config: ProviderPollingConfig;
  private connectionStatus: Record<string, boolean> = {};
  private lastFetchTime: number = 0;
  private isFetching: boolean = false;

  constructor(config: ProviderPollingConfig) {
    this.config = config;
  }

  // Fetch actual connection status from backend API (source of truth)
  private async fetchConnectionStatusFromAPI(): Promise<Record<string, boolean>> {
    // Avoid fetching too frequently (debounce to 2 seconds)
    const now = Date.now();
    if (this.isFetching || (now - this.lastFetchTime < 2000)) {
      return this.connectionStatus;
    }

    this.isFetching = true;
    this.lastFetchTime = now;

    try {
      const response = await fetch('/api/user-tokens');
      
      // If redirected to sign-in, user not authenticated yet
      if (response.redirected || !response.ok) {
        return this.connectionStatus;
      }

      const tokens = await response.json();
      
      // Handle error responses
      if (tokens.error) {
        return this.connectionStatus;
      }
      
      // Update connection status based on API response
      const newStatus: Record<string, boolean> = {};
      
      if (Array.isArray(tokens)) {
        tokens.forEach((token: any) => {
          const provider = token.provider?.toLowerCase();
          if (provider) {
            newStatus[provider] = true;
            // Sync to localStorage for backwards compatibility
            const key = `is${provider.charAt(0).toUpperCase() + provider.slice(1)}Connected`;
            localStorage.setItem(key, 'true');
          }
        });
      }

      // Update internal state
      this.connectionStatus = newStatus;
      return newStatus;
    } catch (error) {
      console.error('[ProviderPolling] Error fetching connection status:', error);
      return this.connectionStatus;
    } finally {
      this.isFetching = false;
    }
  }

  // Get connection status (from internal cache, fallback to localStorage)
  private getConnectionStatus(provider: string): boolean {
    // First check internal state from last API fetch
    if (this.connectionStatus[provider] !== undefined) {
      return this.connectionStatus[provider];
    }
    
    // Fallback to localStorage for backwards compatibility
    const key = `is${provider.charAt(0).toUpperCase() + provider.slice(1)}Connected`;
    return localStorage.getItem(key) === "true";
  }

  // Clear connection status cache for a specific provider (called during disconnect)
  clearProviderCache = (providerId: string) => {
    delete this.connectionStatus[providerId];
    // Force a refresh on next check by resetting debounce timer
    this.lastFetchTime = 0;
  }

  // Continuous provider state checking - now uses API as source of truth
  checkProviderStates = async () => {
    // Fetch latest connection status from API and wait for it
    await this.fetchConnectionStatusFromAPI();

    // Now read connection states (API cache will be updated)
    const isGCPConnected = this.getConnectionStatus('gcp');
    const isSetupInProgressLS = localStorage.getItem("gcpSetupInProgress") === "true";
    const isAzureConnected = this.getConnectionStatus('azure');
    const isAzureConnecting = localStorage.getItem("isAzureConnecting") === "true";
    const isAzureFetching = localStorage.getItem("isAzureFetching") === "true";
    const isAWSConnected = this.getConnectionStatus('aws');
    const isAWSFetching = localStorage.getItem("isAWSFetching") === "true";
    const isOVHConnected = this.getConnectionStatus('ovh');
    const isOVHFetching = localStorage.getItem("isOVHFetching") === "true";
    const isScalewayConnected = this.getConnectionStatus('scaleway');
    const isScalewayFetching = localStorage.getItem("isScalewayFetching") === "true";
    const isTailscaleConnected = this.getConnectionStatus('tailscale');
    const isGrafanaConnected = this.getConnectionStatus('grafana');
    const isDatadogConnected = this.getConnectionStatus('datadog');
    const isNetdataConnected = this.getConnectionStatus('netdata');
    const isKubectlConnected = this.getConnectionStatus('kubectl');
    const isSlackConnected = this.getConnectionStatus('slack');
    const isSplunkConnected = this.getConnectionStatus('splunk');

    this.config.onProvidersUpdate(prev => {
      let hasChanges = false;
      const next = prev.map(provider => {
        switch (provider.id) {
          case 'gcp':
            const newGcpState = {
              ...provider,
              isConnected: isGCPConnected,
              isRefreshing: isSetupInProgressLS
            };
            if (JSON.stringify(newGcpState) !== JSON.stringify(provider)) hasChanges = true;
            return newGcpState;

          case 'azure':
            const newAzureState = {
              ...provider,
              isConnected: isAzureConnected,
              isRefreshing: isAzureConnecting || isAzureFetching
            };
            if (JSON.stringify(newAzureState) !== JSON.stringify(provider)) hasChanges = true;
            return newAzureState;

          case 'aws':
            const newAwsState = {
              ...provider,
              isConnected: isAWSConnected,
              isRefreshing: isAWSFetching
            };
            if (JSON.stringify(newAwsState) !== JSON.stringify(provider)) hasChanges = true;
            return newAwsState;

          case 'ovh':
            const newOvhState = {
              ...provider,
              isConnected: isOVHConnected,
              isRefreshing: isOVHFetching,
              status: isOVHConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newOvhState) !== JSON.stringify(provider)) hasChanges = true;
            return newOvhState;
          case 'scaleway':
            const newScalewayState = {
              ...provider,
              isConnected: isScalewayConnected,
              isRefreshing: isScalewayFetching,
              status: isScalewayConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newScalewayState) !== JSON.stringify(provider)) hasChanges = true;
            return newScalewayState;
          case 'tailscale':
            const newTailscaleState = {
              ...provider,
              isConnected: isTailscaleConnected,
              status: isTailscaleConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newTailscaleState) !== JSON.stringify(provider)) hasChanges = true;
            return newTailscaleState;
          case 'grafana':
            const newGrafanaState = {
              ...provider,
              isConnected: isGrafanaConnected,
              status: isGrafanaConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newGrafanaState) !== JSON.stringify(provider)) hasChanges = true;
            return newGrafanaState;
          case 'datadog':
            const newDatadogState = {
              ...provider,
              isConnected: isDatadogConnected,
              status: isDatadogConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newDatadogState) !== JSON.stringify(provider)) hasChanges = true;
            return newDatadogState;
          case 'netdata':
            const newNetdataState = {
              ...provider,
              isConnected: isNetdataConnected,
              status: isNetdataConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newNetdataState) !== JSON.stringify(provider)) hasChanges = true;
            return newNetdataState;
          case 'kubectl':
            const newKubectlState = {
              ...provider,
              isConnected: isKubectlConnected,
              status: isKubectlConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newKubectlState) !== JSON.stringify(provider)) hasChanges = true;
            return newKubectlState;
          case 'slack':
            const newSlackState = {
              ...provider,
              isConnected: isSlackConnected,
              status: isSlackConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newSlackState) !== JSON.stringify(provider)) hasChanges = true;
            return newSlackState;
          case 'splunk':
            const newSplunkState = {
              ...provider,
              isConnected: isSplunkConnected,
              status: isSplunkConnected ? ('connected' as const) : ('disconnected' as const)
            };
            if (JSON.stringify(newSplunkState) !== JSON.stringify(provider)) hasChanges = true;
            return newSplunkState;

          default:
            return provider;
        }
      });

      // Only return new state if something actually changed      
      return hasChanges ? next : prev;
    });

    // Auto-select logic removed - handled by database-first approach in legitimate connection events only
    // Polling should only monitor connection status, not modify user preferences

    // Synchronize GCP setup progress
    this.config.onSetupProgressUpdate(isSetupInProgressLS);

    // Check if any provider data needs to be fetched (exact same logic as old implementation)
    if (typeof window !== "undefined") {
      // Check if GCP data needs to be fetched
      if (
        localStorage.getItem("isLoggedAurora") === "true" &&
        localStorage.getItem("isGCPConnected") === "true" &&
        localStorage.getItem("isGCPFetched") === "false" &&
        localStorage.getItem("isGCPFetching") !== "true"
      ) {
        this.config.fetchProviderData('gcp');
      }

      // Check if AWS data needs to be fetched
      if (
        localStorage.getItem("isLoggedAurora") === "true" &&
        localStorage.getItem("isAWSConnected") === "true" &&
        localStorage.getItem("isAWSFetched") === "false" &&
        localStorage.getItem("isAWSFetching") !== "true" &&
        localStorage.getItem("cloudProvider") === "aws" // Only fetch if AWS is the current provider
      ) {
        this.config.fetchProviderData('aws');
      }

      // Check if Azure data needs to be fetched
      if (
        localStorage.getItem("isLoggedAurora") === "true" &&
        localStorage.getItem("isAzureConnected") === "true" &&
        localStorage.getItem("isAzureFetched") === "false" &&
        localStorage.getItem("isAzureFetching") !== "true" &&
        localStorage.getItem("cloudProvider") === "azure" // Only fetch if Azure is the current provider
      ) {
        this.config.fetchProviderData('azure');
      }

      if (
        localStorage.getItem('isLoggedAurora') === 'true' &&
        localStorage.getItem('isGrafanaConnected') === 'true'
      ) {
        this.config.fetchProviderData('grafana');
      }
    }
  };

  // Start event-driven monitoring (no polling)
  startPolling = () => {
    // Initial state check (async but don't await - let it run in background)
    this.checkProviderStates().catch(err => 
      console.error('[ProviderPolling] Initial state check failed:', err)
    );

    // Listen for localStorage changes (when other tabs/pages update provider states)
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key && (
        e.key === 'isAWSFetching' ||
        e.key === 'isAzureFetching' ||
        e.key === 'isOVHFetching' ||
        e.key === 'isScalewayFetching' ||
        e.key === 'isAzureConnecting' ||
        e.key === 'isAWSConnected' ||
        e.key === 'isAzureConnected' ||
        e.key === 'isOVHConnected' ||
        e.key === 'isScalewayConnected' ||
        e.key === 'isTailscaleConnected' ||
        e.key === 'isGrafanaConnected' ||
        e.key === 'isDatadogConnected' ||
        e.key === 'isNetdataConnected' ||
        e.key === 'isKubectlConnected' ||
        e.key === 'isSlackConnected' ||
        e.key === 'isSplunkConnected' ||
        e.key === 'isGCPConnected' ||
        e.key === 'isAzureFetched' ||
        e.key === 'isOVHFetched' ||
        e.key === 'isScalewayFetched'
      )) {
        // Immediately check provider states when relevant localStorage keys change
        this.checkProviderStates().catch(err =>
          console.error('[ProviderPolling] State check failed:', err)
        );
      }
    };

    // Listen for custom events that signal provider state changes (for same-tab updates)
    const handleProviderStateChange = () => {
      this.checkProviderStates().catch(err =>
        console.error('[ProviderPolling] State change check failed:', err)
      );
    };

    window.addEventListener('storage', handleStorageChange);
    window.addEventListener('providerStateChanged', handleProviderStateChange);

    // Store cleanup functions
    this.cleanup = () => {
      window.removeEventListener('storage', handleStorageChange);
      window.removeEventListener('providerStateChanged', handleProviderStateChange);
    };
  };

  // GCP Setup Polling methods
  pollGcpSetupStatus = async (taskId: string) => {
    // Prevent multiple polling intervals for the same task
    const existingTaskId = localStorage.getItem("gcpSetupTaskId");
    const pollingInProgress = localStorage.getItem("gcpPollingActive");
    const setupInProgress = localStorage.getItem("gcpSetupInProgress");

    // Don't start polling if setup was cancelled/disconnected
    if (setupInProgress !== "true" && !existingTaskId) {
      return;
    }

    if (pollingInProgress === "true" || (existingTaskId && existingTaskId !== taskId)) {
      return;
    }

    // Mark polling as active
    localStorage.setItem("gcpPollingActive", "true");

    // Set GCP to refreshing state and setup in progress
    this.config.onProvidersUpdate(prev => prev.map(p => p.id === 'gcp' ? { ...p, isRefreshing: true } : p));
    this.config.onSetupProgressUpdate(true);
    localStorage.setItem("gcpSetupInProgress", "true");
    localStorage.setItem("gcpSetupInProgress_timestamp", Date.now().toString());

    const maxAttempts = 360; // 30 minutes with 5-second intervals (for 200+ projects)
    let attempts = 0;

    const intervalId = setInterval(async () => {
      attempts++;

      // Check if user has cancelled/disconnected - stop polling immediately
      const setupInProgress = localStorage.getItem("gcpSetupInProgress");
      const currentTaskId = localStorage.getItem("gcpSetupTaskId");
      if (setupInProgress !== "true" || currentTaskId !== taskId) {
        clearInterval(intervalId);
        localStorage.removeItem("gcpPollingActive");
        localStorage.removeItem("gcpPollingIntervalId");
        return;
      }

      try {
        const response = await fetch(`${BACKEND_URL}/gcp/setup/status/${taskId}`);
        const data = await response.json();
        console.log(`GCP setup status attempt ${attempts}:`, data);

        // Update provider with detailed progress information
        if (data.status && data.progress !== undefined) {
          this.config.onProvidersUpdate(prev => prev.map(p =>
            p.id === 'gcp' ? {
              ...p,
              isRefreshing: true,
              setupStatus: {
                message: data.status,
                progress: data.progress,
                step: data.step || 0,
                totalSteps: data.total_steps || 7,
                propagation: data.propagation
              }
            } : p
          ));
        }

        if (data.complete) {
          if (data.state === 'SUCCESS') {
            const result = data.result || {};
            const redirectParams = result.redirect_params || "";
            const status = result.status;

            if (redirectParams.includes('login=gcp_no_projects') ||
              redirectParams.includes('login=gcp_failed_billing') ||
              status === 'FAILED') {
              this.handleGcpSetupFailure(redirectParams, status);
            } else {
              console.log(`GCP setup completed successfully`);
              // Check if project selection is needed
              console.log('Checking for project selection need:', data.result);
              if (data.result?.status === 'needs_selection') {
                // Persist selection state across page navigation
                localStorage.setItem("gcpNeedsProjectSelection", "true");
                localStorage.setItem("gcpEligibleProjects", JSON.stringify(data.result.eligible_projects));
                
                // Stop polling and clear all intervals
                clearInterval(intervalId);
                if (this.gcpSetupInterval) {
                  clearInterval(this.gcpSetupInterval);
                  this.gcpSetupInterval = null;
                }
                localStorage.removeItem("gcpPollingIntervalId");
                localStorage.removeItem("gcpPollingActive");
                
                // Clear setup progress state but keep selection state
                localStorage.removeItem("gcpSetupInProgress");
                localStorage.removeItem("gcpSetupInProgress_timestamp");
                localStorage.removeItem("gcpSetupTaskId");
                
                // Update UI state
                this.config.onProvidersUpdate(prev => prev.map(p => p.id === 'gcp' ? { ...p, isRefreshing: false } : p));
                this.config.onSetupProgressUpdate(false);
                
                // Dispatch event AFTER state is cleaned up
                window.dispatchEvent(new Event("gcpProjectSelectionNeeded"));
                return;
              }
              console.log('GCP setup completed successfully');
              
              // Check propagation status and warn if partial
              const propagationStatus = result.propagation_status || 'FULL';
              if (propagationStatus === 'PARTIAL') {
                if (this.config.onError) {
                  this.config.onError({
                    message: `GCP IAM propagation partial (${result.verified_projects}/${result.total_projects} projects accessible). Remaining projects may take additional time.`,
                    action: 'open_console'
                  }, 10000);
                }
              }
              
              this.completeGcpSetup(true);
            }
          } else {
            console.log(`GCP setup failed with state: ${data.state}`);
            if (this.config.onError) {
              this.config.onError(`GCP setup failed (${data.state})`);
            }
            this.completeGcpSetup(false);
            this.disconnectGcp();
          }
        } else if (attempts >= maxAttempts) {
          console.log(`GCP setup polling timeout after ${maxAttempts} attempts`);
          this.completeGcpSetup(false);
        }
      } catch (error) {
        console.error(`GCP setup status check failed on attempt ${attempts}:`, error);
        if (attempts >= maxAttempts) {
          this.completeGcpSetup(false);
        }
      }
    }, 5000); // Poll every 5 seconds

    // Store both in instance and localStorage for cleanup
    this.gcpSetupInterval = intervalId;
    localStorage.setItem("gcpPollingIntervalId", intervalId.toString());
  };

  stopGcpPolling = () => {
    // Clear from instance reference
    if (this.gcpSetupInterval) {
      clearInterval(this.gcpSetupInterval);
      this.gcpSetupInterval = null;
    }

    // Clear any orphaned interval from localStorage
    const storedIntervalId = localStorage.getItem("gcpPollingIntervalId");
    if (storedIntervalId) {
      clearInterval(Number(storedIntervalId));
      localStorage.removeItem("gcpPollingIntervalId");
    }

    localStorage.removeItem("gcpPollingActive");
  };

  // Helper to handle GCP setup failures with appropriate error messages
  private handleGcpSetupFailure = (redirectParams: string, status: string | undefined) => {
    if (redirectParams.includes('login=gcp_no_projects')) {
      console.log(`GCP setup failed: No projects found`);
      if (this.config.onError) {
        this.config.onError({ message: "No GCP projects found. Please create one in Google Cloud Console.", action: 'open_console' }, 10000);
      }
    } else if (redirectParams.includes('login=gcp_failed_billing')) {
      console.log(`GCP setup failed: No billing enabled`);
      if (this.config.onError) {
        this.config.onError({ message: "None of your GCP projects have billing enabled. Please enable billing to use Aurora.", action: 'open_console' }, 10000);
      }
    } else if (status === 'FAILED') {
      console.log(`GCP setup failed with generic error`);
      if (this.config.onError) {
        this.config.onError("GCP connection failed during setup.", 10000);
      }
    }
    this.completeGcpSetup(false);
    this.disconnectGcp();
  };

  // Helper to clean up invalid GCP state on backend
  private disconnectGcp = async () => {
    try {
      const response = await fetch(`/api/connected-accounts/gcp`, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' }
      });
      if (response.ok) {
        console.log('Disconnected GCP due to setup failure');
        // Notify UI of disconnect
        this.config.onProvidersUpdate(prev => prev.map(p =>
          p.id === 'gcp' ? { ...p, isConnected: false, status: 'disconnected' as const, projects: [] } : p
        ));
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }
    } catch (e) {
      console.error('Failed to disconnect GCP:', e);
    }
  };

  private completeGcpSetup = (success: boolean) => {
    // Clear interval from instance
    if (this.gcpSetupInterval) {
      clearInterval(this.gcpSetupInterval);
      this.gcpSetupInterval = null;
    }

    // Also clear any orphaned interval from localStorage
    const storedIntervalId = localStorage.getItem("gcpPollingIntervalId");
    if (storedIntervalId) {
      clearInterval(Number(storedIntervalId));
      localStorage.removeItem("gcpPollingIntervalId");
    }

    localStorage.removeItem("gcpPollingActive");

    if (success) {
      localStorage.setItem("isGCPConnected", "true");
      localStorage.setItem("cloudProvider", "gcp");
      localStorage.setItem("isGCPFetched", "false");
      localStorage.setItem("isLoggedAurora", "true");

      // Update provider state first and clear setup status
      this.config.onProvidersUpdate(prev => prev.map(p =>
        p.id === 'gcp' ? {
          ...p,
          isRefreshing: false,
          isConnected: true,
          setupStatus: undefined  // Clear progress on completion
        } : p
      ));

      // Dispatch storage event to notify hooks immediately
      window.dispatchEvent(new CustomEvent('providerStateChanged'));

      // Auto-select provider (this should trigger UI update)
      this.config.autoSelectProvider("gcp", true);

      setTimeout(() => {
        this.config.onSetupProgressUpdate(false);
        localStorage.removeItem("gcpSetupInProgress");
        localStorage.removeItem("gcpSetupInProgress_timestamp");
        localStorage.removeItem("gcpSetupTaskId");
      }, 1000);
    } else {
      this.config.onProvidersUpdate(prev => prev.map(p =>
        p.id === 'gcp' ? {
          ...p,
          isRefreshing: false,
          setupStatus: undefined  // Clear progress on failure
        } : p
      ));
      this.config.onSetupProgressUpdate(false);
      localStorage.removeItem("gcpSetupInProgress");
      localStorage.removeItem("gcpSetupInProgress_timestamp");
      localStorage.removeItem("gcpSetupTaskId");
    }
  };

  resumePollingIfNeeded = () => {
    const setupInProgress = localStorage.getItem("gcpSetupInProgress") === "true";
    const taskId = localStorage.getItem("gcpSetupTaskId");
    const pollingActive = localStorage.getItem("gcpPollingActive") === "true";

    if (setupInProgress && taskId && !pollingActive) {
      console.log(`Resuming GCP setup polling for task: ${taskId}`);
      this.pollGcpSetupStatus(taskId);
    } else if (setupInProgress && taskId) {
      // Do a single immediate check to see if task completed while away
      console.log(`GCP setup in progress - checking if task ${taskId} completed while away`);
      fetch(`${BACKEND_URL}/gcp/setup/status/${taskId}`)
        .then(response => response.json())
        .then(data => {
          console.log(`Immediate task check result:`, data);
          if (data.complete && data.state === 'SUCCESS') {
            // Check if project selection is needed
            if (data.result?.status === 'needs_selection') {
              // Persist selection state across page navigation
              localStorage.setItem("gcpNeedsProjectSelection", "true");
              localStorage.setItem("gcpEligibleProjects", JSON.stringify(data.result.eligible_projects));
              
              // Dispatch event so GlobalProjectSelectionMonitor can detect it
              window.dispatchEvent(new Event("gcpProjectSelectionNeeded"));
              
              // Clean up polling state
              this.config.onSetupProgressUpdate(false);
              localStorage.removeItem("gcpSetupInProgress");
              localStorage.removeItem("gcpSetupInProgress_timestamp");
              localStorage.removeItem("gcpSetupTaskId");
              localStorage.removeItem("gcpPollingActive");
              return;
            }
            
            const result = data.result || {};
            const redirectParams = result.redirect_params || "";
            const status = result.status;

            // Check if it's actually a failure despite Celery task completing
            if (redirectParams.includes('login=gcp_no_projects') ||
              redirectParams.includes('login=gcp_failed_billing') ||
              status === 'FAILED') {
              console.log(`GCP setup failed while away - cleaning up`);
              this.handleGcpSetupFailure(redirectParams, status);
              return;
            }

            console.log(`GCP setup completed while away - cleaning up`);
            localStorage.setItem("isGCPConnected", "true");
            localStorage.setItem("cloudProvider", "gcp");
            localStorage.setItem("isGCPFetched", "false");
            localStorage.setItem("isLoggedAurora", "true");
            this.config.autoSelectProvider("gcp", true);
            this.config.onProvidersUpdate(prev => prev.map(p => p.id === 'gcp' ? { ...p, isRefreshing: false, isConnected: true } : p));

            this.config.onSetupProgressUpdate(false);
            localStorage.removeItem("gcpSetupInProgress");
            localStorage.removeItem("gcpSetupInProgress_timestamp");
            localStorage.removeItem("gcpSetupTaskId");
            localStorage.removeItem("gcpPollingActive");
          } else if (!data.complete) {
            console.log(`Task still running - forcing polling resume`);
            localStorage.removeItem("gcpPollingActive");
            this.pollGcpSetupStatus(taskId);
          }
        })
        .catch(error => {
          console.log(`Immediate task check failed:`, error);
          localStorage.removeItem("gcpPollingActive");
          this.pollGcpSetupStatus(taskId);
        });
    }
  };

  // Provider data fetching functions
  fetchProviderData = async (providerId: string) => {
    console.log(`Fetching ${providerId} data...`);

    // Set refreshing state
    this.config.onProvidersUpdate(prev => prev.map(p =>
      p.id === providerId ? { ...p, isRefreshing: true } : p
    ));

    // Set fetching flag in localStorage with timestamp
    localStorage.setItem(`is${providerId.toUpperCase()}Fetching`, "true");
    localStorage.setItem(`is${providerId.toUpperCase()}Fetching_timestamp`, Date.now().toString());
    // Dispatch event to notify other instances
    window.dispatchEvent(new CustomEvent('providerStateChanged'));

    try {
      let endpoint = '';
      let method = 'POST';
      let body: any = {};

      // Get userId for API calls
      const userResponse = await fetch('/api/getUserId');
      if (!userResponse.ok) return;
      const userData = await userResponse.json();
      const userId = userData.userId;
      if (!userId) return;

      switch (providerId) {
        case 'gcp':
          endpoint = `${BACKEND_URL}/billing`;
          body = { userId };
          break;
        case 'aws':
          endpoint = `${BACKEND_URL}/aws_api/get-credentials`;
          body = { userId };
          break;
        case 'azure':
          // Azure uses token validation
          const tokenResponse = await fetch(`/api/user-tokens`);
          if (!tokenResponse.ok) {
            console.error("Failed to fetch user tokens for Azure");
            return;
          }
          endpoint = `${BACKEND_URL}/azure/subscriptions`;
          body = { userId };
          break;
      }

      const response = await fetch(endpoint, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-User-ID": userId
        },
        ...(method === 'POST' && { body: JSON.stringify(body) }),
        credentials: 'include'
      });

      if (response.ok) {
        // Mark as fetched
        localStorage.setItem(`is${providerId.toUpperCase()}Fetched`, "true");
        localStorage.removeItem(`is${providerId.toUpperCase()}Fetching`);
        localStorage.removeItem(`is${providerId.toUpperCase()}Fetching_timestamp`);

        this.config.onProvidersUpdate(prev => prev.map(p =>
          p.id === providerId ? {
            ...p,
            isRefreshing: false,
            isConnected: true,
            lastSync: new Date().toISOString()
          } : p
        ));

        console.log(`${providerId} data fetched successfully`);
      } else {
        throw new Error(`Failed to fetch ${providerId} data: ${response.status}`);
      }
    } catch (error) {
      console.error(`Error fetching ${providerId} data:`, error);
      localStorage.removeItem(`is${providerId.toUpperCase()}Fetching`);
      localStorage.removeItem(`is${providerId.toUpperCase()}Fetching_timestamp`);
      this.config.onProvidersUpdate(prev => prev.map(p =>
        p.id === providerId ? { ...p, isRefreshing: false } : p
      ));
    }

    // Dispatch event to notify other instances
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  // Stop polling and cleanup
  stopPolling = () => {
    if (this.cleanup) {
      this.cleanup();
      this.cleanup = null;
    }
    if (this.gcpSetupInterval) {
      clearInterval(this.gcpSetupInterval);
      this.gcpSetupInterval = null;
    }
  };
}
