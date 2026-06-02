import { useState, useCallback } from 'react';
import { useToast } from '@/hooks/use-toast';
import { setRootProject } from './projectUtils';

interface UseSetAsRootResult {
  setAsRoot: (providerId: string, projectId: string) => Promise<void>;
  isSaving: boolean;
}

export function useSetAsRoot(
  userId: string | null,
  refresh: () => Promise<void>,
  resourceLabel: string = 'project',
): UseSetAsRootResult {
  const [isSaving, setIsSaving] = useState(false);
  const { toast } = useToast();

  const setAsRoot = useCallback(
    async (providerId: string, projectId: string) => {
      if (!userId) return;
      setIsSaving(true);
      try {
        await setRootProject(providerId, projectId);
        await refresh();
        toast({
          title: 'Success',
          description: `Root ${resourceLabel} updated successfully`,
        });
      } catch (error: unknown) {
        const message =
          error instanceof Error ? error.message : `Failed to set root ${resourceLabel}`;
        console.error(`Error setting root ${resourceLabel}:`, error);
        toast({
          title: 'Error',
          description: message,
          variant: 'destructive',
        });
      } finally {
        setIsSaving(false);
      }
    },
    [userId, refresh, toast, resourceLabel],
  );

  return { setAsRoot, isSaving };
}
