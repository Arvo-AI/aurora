'use client';

import React from 'react';
import { CreditCard, Loader2, RefreshCw } from 'lucide-react';
import { useUser } from '@/hooks/useAuthHooks';
import { useUsageData } from '@/hooks/useUsageData';


export default function AuroraCreditProgressBar() {
  const { user, isLoaded: authLoaded } = useUser();
  const { usageData, loading: usageLoading, error: usageError, refetch } = useUsageData();

  // Get API cost from usage data
  const getApiCost = (): number => {
    if (!usageData?.billing_summary?.total_api_cost) {
      return 0;
    }

    return Math.max(0, usageData.billing_summary.total_api_cost);
  };

  const apiCost = getApiCost();
  
  // Show loading only during initial load when we have no data yet
  // Don't hide component during refresh if we already have data
  const isInitialLoading = !authLoaded || (usageLoading && !usageData);

  // Show loading state during initial load
  if (isInitialLoading) {
    return (
      <div className="p-3 rounded-lg border border-muted bg-muted/30 space-y-2">
        <div className="flex items-center gap-2">
          <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
          <span className="text-xs text-muted-foreground">Loading API cost...</span>
        </div>
      </div>
    );
  }

  // Show error state
  if (usageError && !usageData) {
    return (
      <div className="p-3 rounded-lg border border-red-200 bg-red-50 space-y-2">
        <div className="flex items-center gap-2">
          <CreditCard className="h-3 w-3 text-red-500" />
          <span className="text-xs text-red-600">
            {usageError || 'Failed to load API cost'}
          </span>
        </div>
      </div>
    );
  }

  // Show nothing if no data
  if (!usageData) {
    return null;
  }

  return (
    <div className="p-3 rounded-lg border border-gray-600 bg-gray-800/50 text-sm">
      <div className="flex items-center gap-2">
        <CreditCard className="h-4 w-4 text-gray-400" />
        <span className="text-white font-medium">
          API Cost: ${apiCost.toFixed(2)}
        </span>
      </div>
      
      <button
        onClick={refetch}
        disabled={usageLoading}
        className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-300 transition-colors disabled:opacity-50 mt-2"
      >
        <RefreshCw className={`h-3 w-3 ${usageLoading ? 'animate-spin' : ''}`} />
        {usageLoading ? 'Refreshing...' : 'Refresh'}
      </button>
    </div>
  );
}