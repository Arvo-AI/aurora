// ============================================================================
// User Preferences Service
// ============================================================================

export interface AuroraLearnSetting {
  enabled: boolean;
}

/**
 * Get the Aurora Learn setting for the current user.
 * Defaults to true if not set.
 */
export async function getAuroraLearnSetting(): Promise<AuroraLearnSetting> {
  const response = await fetch('/api/user/preferences/aurora-learn', {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Failed to get Aurora Learn setting: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Set the Aurora Learn setting for the current user.
 */
export async function setAuroraLearnSetting(
  enabled: boolean
): Promise<{ success: boolean; enabled: boolean }> {
  const response = await fetch('/api/user/preferences/aurora-learn', {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ enabled }),
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Failed to set Aurora Learn setting: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Convenience object for importing all preference functions.
 */
export const userPreferencesService = {
  getAuroraLearnSetting,
  setAuroraLearnSetting,
};
