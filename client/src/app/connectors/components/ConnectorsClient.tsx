"use client";

import React, { useState, useMemo, useEffect } from "react";
import ConnectorGrid from "@/components/connectors/ConnectorGrid";
import ConnectorHeader from "@/components/connectors/ConnectorHeader";
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry";
import type { ConnectorConfig } from "@/components/connectors/types";
import { isOvhEnabled } from "@/lib/feature-flags";

// Helper function to fetch API statuses for custom connection connectors
async function fetchApiStatuses(customConnectionConnectors: ConnectorConfig[]): Promise<Record<string, boolean>> {
  if (customConnectionConnectors.length === 0) return {};
  
  try {
    const response = await fetch('/api/connected-accounts', {
      credentials: 'include',
    });
    if (!response.ok) return {};
    
    const data = await response.json();
    const accounts = data.accounts || {};
    const apiStatuses: Record<string, boolean> = {};
    
    customConnectionConnectors.forEach((connector) => {
      const isConnectedInDb = Object.keys(accounts).some(
        key => key.toLowerCase() === connector.id.toLowerCase()
      );
      apiStatuses[connector.id] = isConnectedInDb;
    });
    
    return apiStatuses;
  } catch (error) {
    console.error('Error fetching connected accounts from API:', error);
    return {};
  }
}

// Helper function to check if user has VM config (manual or auto VMs)
async function checkVmConfigStatus(): Promise<boolean> {
  try {
    const manualResponse = await fetch('/api/vms/manual', {
      credentials: 'include',
    });
    
    if (manualResponse.ok) {
      const manualData = await manualResponse.json();
      if (manualData.vms && manualData.vms.length > 0) {
        return true;
      }
    }
    
    // Check for auto VMs (OVH and Scaleway)
    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL;
    if (!backendUrl) return false;
    
    // Get user ID for auto VM checks
    const userResponse = await fetch('/api/getUserId', {
      credentials: 'include',
    });
    if (!userResponse.ok) return false;
    
    const userData = await userResponse.json();
    const userId = userData.userId;
    if (!userId) return false;
    
    // Check OVH instances (only if feature flag is enabled)
    if (isOvhEnabled()) {
      try {
        const ovhResponse = await fetch(`${backendUrl}/ovh_api/ovh/instances`, {
          headers: { "X-User-ID": userId },
          credentials: "include",
        });
        if (ovhResponse.ok) {
          const ovhData = await ovhResponse.json();
          if (ovhData.instances && ovhData.instances.length > 0) {
            return true;
          }
        }
      } catch (err) {
        // Silently fail - continue checking Scaleway
      }
    }
    
    // Check Scaleway instances
    try {
      const scwResponse = await fetch(`${backendUrl}/scaleway_api/scaleway/instances`, {
        headers: { "X-User-ID": userId },
        credentials: "include",
      });
      if (scwResponse.ok) {
        const scwData = await scwResponse.json();
        if (scwData.servers && scwData.servers.length > 0) {
          return true;
        }
      }
    } catch (err) {
      // Silently fail
    }
    
    return false;
  } catch (error) {
    console.error('Error checking VM config status:', error);
    return false;
  }
}

// Helper function to sync localStorage with connection status
function syncLocalStorage(connectorId: string, connectorName: string, isConnected: boolean, storageKey?: string): void {
  const key = storageKey || `is${connectorName}Connected`;
  if (isConnected) {
    localStorage.setItem(key, "true");
  } else {
    localStorage.removeItem(key);
  }
}

// Helper function to check status for a single connector
async function checkConnectorStatus(
  connector: ConnectorConfig,
  apiStatuses: Record<string, boolean>,
  vmConfigStatus?: boolean
): Promise<boolean> {
  // On Prem is considered connected when user has VM config setup
  if (connector.id === "onprem") {
    const hasVmConfig = vmConfigStatus ?? await checkVmConfigStatus();
    syncLocalStorage(connector.id, connector.name, hasVmConfig, connector.storageKey);
    return hasVmConfig;
  }
  
  // For connectors with useCustomConnection (like GCP), use API status if available
  if (connector.useCustomConnection && connector.id in apiStatuses) {
    const isConnected = apiStatuses[connector.id];
    syncLocalStorage(connector.id, connector.name, isConnected, connector.storageKey);
    return isConnected;
  }
  
  // For GitHub, check cached data with expiration validation
  if (connector.id === "github") {
    const cachedData = localStorage.getItem('github_cached_data');
    const lastChecked = localStorage.getItem('github_last_checked');
    
    if (cachedData && lastChecked) {
      try {
        const now = Date.now();
        const cacheAge = now - parseInt(lastChecked, 10);
        const CACHE_VALIDITY_MS = 300000; // 5 minutes
        
        // Only use cache if it's less than 5 minutes old
        if (cacheAge < CACHE_VALIDITY_MS) {
          const data = JSON.parse(cachedData);
          return data.connected || false;
        }
        // Cache expired, return false to indicate not connected
        return false;
      } catch (error) {
        return false;
      }
    }
    // No cache available
    return false;
  }
  
  // For other connectors, use their storage key
  const storageKey = connector.storageKey || `is${connector.name}Connected`;
  return localStorage.getItem(storageKey) === "true";
}

export default function ConnectorsClient() {
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [connectedStatus, setConnectedStatus] = useState<Record<string, boolean>>({});
  const [isLoading, setIsLoading] = useState(true);

  const allConnectors = useMemo(() => connectorRegistry.getAll(), []);

  // Check connection status for all connectors
  useEffect(() => {
    const checkAllConnections = async () => {
      if (typeof window === "undefined") return;
      
      setIsLoading(true);
      const customConnectionConnectors = allConnectors.filter(c => c.useCustomConnection);
      const apiStatuses = await fetchApiStatuses(customConnectionConnectors);
      
      const vmConfigStatus = await checkVmConfigStatus();
      
      const status: Record<string, boolean> = {};
      for (const connector of allConnectors) {
        status[connector.id] = await checkConnectorStatus(connector, apiStatuses, vmConfigStatus);
      }
      
      setConnectedStatus(status);
      setIsLoading(false);
    };

    checkAllConnections();

    const handleProviderChange = () => checkAllConnections();
    window.addEventListener("providerStateChanged", handleProviderChange);
    
    return () => window.removeEventListener("providerStateChanged", handleProviderChange);
  }, []);

  // Get unique categories from all connectors
  const availableCategories = useMemo(() => {
    const categories = new Set<string>();
    allConnectors.forEach((connector) => {
      if (connector.category) {
        categories.add(connector.category);
      }
    });
    return Array.from(categories).sort();
  }, [allConnectors]);

  // Filter connectors based on search query and selected categories
  const filteredConnectors = useMemo(() => {
    let filtered = allConnectors;

    // Apply category filter
    if (selectedCategories.length > 0) {
      filtered = filtered.filter((connector) =>
        connector.category && selectedCategories.includes(connector.category)
      );
    }

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (connector) =>
          connector.name.toLowerCase().includes(query) ||
          connector.description.toLowerCase().includes(query) ||
          (connector.category && connector.category.toLowerCase().includes(query))
      );
    }

    return filtered;
  }, [allConnectors, searchQuery, selectedCategories]);

  // Split connectors into installed and available
  const { installedConnectors, availableConnectors } = useMemo(() => {
    const installed = filteredConnectors.filter((connector) => connectedStatus[connector.id]);
    const available = filteredConnectors.filter((connector) => !connectedStatus[connector.id]);
    
    return {
      installedConnectors: installed,
      availableConnectors: available,
    };
  }, [filteredConnectors, connectedStatus]);

  const handleCategoryToggle = (category: string) => {
    setSelectedCategories((prev) =>
      prev.includes(category)
        ? prev.filter((c) => c !== category)
        : [...prev, category]
    );
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="container mx-auto py-8 px-4 max-w-7xl">
        <ConnectorHeader
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          selectedCategories={selectedCategories}
          onCategoryToggle={handleCategoryToggle}
          availableCategories={availableCategories}
        />
        
        {isLoading ? (
          <div className="flex items-center justify-center py-16">
            <div className="text-muted-foreground">Loading connectors...</div>
          </div>
        ) : (
          <>
            {/* Installed Section */}
            {installedConnectors.length > 0 && (
              <div className="mb-8">
                <div className="border-b border-green-500 pb-4 mb-6">
                  <h2 className="text-xl font-semibold flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-green-500"></span>
                    Installed
                  </h2>
                </div>
                <ConnectorGrid connectors={installedConnectors} />
              </div>
            )}
            
            {/* Available Section */}
            {availableConnectors.length > 0 && (
              <div>
                <h2 className="text-xl font-semibold mb-4">Available</h2>
                <ConnectorGrid connectors={availableConnectors} />
              </div>
            )}
            
            {/* No Results */}
            {installedConnectors.length === 0 && availableConnectors.length === 0 && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <p className="text-muted-foreground text-lg mb-2">No connectors found</p>
                <p className="text-muted-foreground text-sm">Try adjusting your search or filters</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
