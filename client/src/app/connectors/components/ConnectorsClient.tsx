"use client";

import React, { useState, useMemo, useEffect } from "react";
import ConnectorGrid from "@/components/connectors/ConnectorGrid";
import ConnectorHeader from "@/components/connectors/ConnectorHeader";
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry";

async function fetchAllStatuses(): Promise<Record<string, boolean>> {
  try {
    const res = await fetch("/api/connectors/status", { credentials: "include" });
    if (!res.ok) return {};
    const data = await res.json();
    const connectors: Record<string, { connected?: boolean }> = data.connectors || {};
    const result: Record<string, boolean> = {};
    for (const [id, info] of Object.entries(connectors)) {
      result[id] = info.connected === true;
    }
    return result;
  } catch {
    return {};
  }
}

function syncLocalStorage(connectorId: string, connectorName: string, isConnected: boolean, storageKey?: string): void {
  const key = storageKey || `is${connectorName}Connected`;
  if (isConnected) {
    localStorage.setItem(key, "true");
  } else {
    localStorage.removeItem(key);
  }
}

export default function ConnectorsClient() {
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [connectedStatus, setConnectedStatus] = useState<Record<string, boolean>>({});
  const [isLoading, setIsLoading] = useState(true);

  const allConnectors = useMemo(() => connectorRegistry.getAll(), []);

  useEffect(() => {
    const loadStatuses = async () => {
      if (typeof window === "undefined") return;
      setIsLoading(true);

      const statuses = await fetchAllStatuses();

      for (const connector of allConnectors) {
        const connected = statuses[connector.id] ?? false;
        syncLocalStorage(connector.id, connector.name, connected, connector.storageKey);
      }

      setConnectedStatus(statuses);
      setIsLoading(false);
    };

    loadStatuses();

    const handleProviderChange = () => loadStatuses();
    window.addEventListener("providerStateChanged", handleProviderChange);
    return () => window.removeEventListener("providerStateChanged", handleProviderChange);
  }, [allConnectors]);

  const availableCategories = useMemo(() => {
    const categories = new Set<string>();
    allConnectors.forEach((connector) => {
      if (connector.category) {
        categories.add(connector.category);
      }
    });
    return Array.from(categories).sort();
  }, [allConnectors]);

  const filteredConnectors = useMemo(() => {
    let filtered = allConnectors;

    if (selectedCategories.length > 0) {
      filtered = filtered.filter((connector) =>
        connector.category && selectedCategories.includes(connector.category)
      );
    }

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
            
            {availableConnectors.length > 0 && (
              <div>
                <h2 className="text-xl font-semibold mb-4">Available</h2>
                <ConnectorGrid connectors={availableConnectors} />
              </div>
            )}
            
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
