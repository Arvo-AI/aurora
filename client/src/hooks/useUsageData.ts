import { useState, useEffect, useCallback } from 'react';
import { useUser } from '@/hooks/useAuthHooks';

interface ModelUsage {
  model_name: string;
  usage_count: number;
  total_cost: number;
  total_surcharge: number;
  total_cost_with_surcharge: number;
  first_used: string | null;
  last_used: string | null;
}

interface BillingSummary {
  total_api_cost: number;
  total_cost: number;
  currency: string;
}

interface UsageData {
  models: ModelUsage[];
  total_models: number;
  billing_summary: BillingSummary;
}

export function useUsageData() {
  const { user } = useUser();
  const [usageData, setUsageData] = useState<UsageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchUsageData = useCallback(async () => {
    if (!user?.id) {
      setLoading(false);
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await fetch('/api/llm-usage/models');

      if (!response.ok) {
        throw new Error(`Failed to fetch usage data: ${response.statusText}`);
      }

      const data = await response.json();
      setUsageData(data);
    } catch (err) {
      console.error('[useUsageData] Error fetching usage data:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch usage data');
    } finally {
      setLoading(false);
    }
  }, [user?.id]);

  useEffect(() => {
    fetchUsageData();
  }, [fetchUsageData]);

  return { usageData, loading, error, refetch: fetchUsageData };
}
