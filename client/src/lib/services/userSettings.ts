/**
 * User Settings Service - handles user preference and settings operations
 */

/**
 * Clear Terraform state files for the current user
 */
export const clearTerraformState = async (): Promise<{
  success: boolean;
  message: string;
  files_cleared?: string[];
  error?: string;
}> => {
  try {
    const response = await fetch('/api/terraform/clear-state', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP ${response.status}: ${response.statusText}`);
    }

    const data = await response.json();
    return data;
  } catch (error) {
    console.error('Error clearing Terraform state:', error);
    return {
      success: false,
      message: 'Failed to clear Terraform state',
      error: error instanceof Error ? error.message : 'Unknown error'
    };
  }
};