import { useQuery, jsonFetcher } from '@/lib/query';
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

  const { data, error, isLoading, mutate } = useQuery<UsageData>(
    user?.id ? '/api/llm-usage/models' : null,
    jsonFetcher,
    {
      staleTime: 60_000,
      retryCount: 3,
      retryDelay: 2000,
      revalidateOnFocus: true,
    },
  );

  return {
    usageData: data ?? null,
    loading: isLoading,
    error: error?.message ?? null,
    refetch: mutate,
  };
}
