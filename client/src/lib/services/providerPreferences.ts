/**
 * Database-first provider preferences service
 * Replaces localStorage-based provider preference management
 */

export interface ProviderPreferencesResponse {
  providers: string[];
  source: 'database' | 'cache';
}

export interface ProviderPreferencesUpdateResponse {
  success: boolean;
  providers: string[];
  action: 'set' | 'add' | 'remove';
  message: string;
  source: 'database';
}

class ProviderPreferencesService {
  private cache: string[] | null = null;
  private cacheTimestamp: number = 0;
  private readonly CACHE_DURATION = 5 * 60 * 1000; // 5 minutes
  private pendingSetRequest: ReturnType<typeof setTimeout> | null = null;
  private lastSetProviders: string[] | null = null;

  /**
   * Get provider preferences from database (with caching)
   */
  async getProviderPreferences(): Promise<string[]> {
    // Check cache first
    if (this.cache && Date.now() - this.cacheTimestamp < this.CACHE_DURATION) {
      return this.cache;
    }

    try {
      const response = await fetch('/api/provider-preferences', {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' }
      });

      if (!response.ok) {
        console.error('Failed to fetch provider preferences:', response.status);
        return this.getFallbackPreferences();
      }

      const data: ProviderPreferencesResponse = await response.json();
      
      // Update cache
      this.cache = data.providers;
      this.cacheTimestamp = Date.now();
      
      // Also update localStorage for quick access
      localStorage.setItem('provider_preferences_cache', JSON.stringify(data.providers));
      
      return data.providers;
    } catch (error) {
      console.error('Error fetching provider preferences:', error);
      return this.getFallbackPreferences();
    }
  }

  async setProviderPreferences(providers: string[]): Promise<boolean> {
    const sortedProviders = [...providers].sort();
    const sortedLast = this.lastSetProviders ? [...this.lastSetProviders].sort() : null;
    
    if (sortedLast && JSON.stringify(sortedProviders) === JSON.stringify(sortedLast)) {
      return true;
    }
    
    this.cache = providers;
    this.cacheTimestamp = Date.now();
    localStorage.setItem('provider_preferences_cache', JSON.stringify(providers));
    
    if (this.pendingSetRequest) {
      clearTimeout(this.pendingSetRequest);
    }
    
    return new Promise((resolve) => {
      this.pendingSetRequest = setTimeout(async () => {
        this.lastSetProviders = providers;
        try {
          const response = await fetch('/api/provider-preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              providers,
              action: 'set'
            })
          });

          if (!response.ok) {
            console.error('Failed to set provider preferences:', response.status);
            resolve(false);
            return;
          }

          const data: ProviderPreferencesUpdateResponse = await response.json();
          resolve(data.success);
        } catch (error) {
          console.error('Error setting provider preferences:', error);
          resolve(false);
        }
      }, 500);
    });
  }

  /**
   * Add a provider to preferences
   */
  async addProvider(providerId: string): Promise<boolean> {
    try {
      const response = await fetch('/api/provider-preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'add',
          provider: providerId
        })
      });

      if (!response.ok) {
        console.error('Failed to add provider preference:', response.status);
        return false;
      }

      const data: ProviderPreferencesUpdateResponse = await response.json();
      
      if (data.success) {
        // Update cache
        this.cache = data.providers;
        this.cacheTimestamp = Date.now();
        
        // Update localStorage cache
        localStorage.setItem('provider_preferences_cache', JSON.stringify(data.providers));
        
        return true;
      }
      
      return false;
    } catch (error) {
      console.error('Error adding provider preference:', error);
      return false;
    }
  }

  /**
   * Remove a provider from preferences
   */
  async removeProvider(providerId: string): Promise<boolean> {
    try {
      const response = await fetch('/api/provider-preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'remove',
          provider: providerId
        })
      });

      if (!response.ok) {
        console.error('Failed to remove provider preference:', response.status);
        return false;
      }

      const data: ProviderPreferencesUpdateResponse = await response.json();
      
      if (data.success) {
        // Update cache
        this.cache = data.providers;
        this.cacheTimestamp = Date.now();
        
        // Update localStorage cache
        localStorage.setItem('provider_preferences_cache', JSON.stringify(data.providers));
        
        
        return true;
      }
      
      return false;
    } catch (error) {
      console.error('Error removing provider preference:', error);
      return false;
    }
  }

  /**
   * Toggle a provider (add if not present, remove if present)
   */
  async toggleProvider(providerId: string): Promise<string[]> {
    const currentPreferences = await this.getProviderPreferences();
    const isSelected = currentPreferences.includes(providerId);
    
    if (isSelected) {
      await this.removeProvider(providerId);
    } else {
      await this.addProvider(providerId);
    }
    
    return await this.getProviderPreferences();
  }

  /**
   * Smart auto-select: Only add provider if it's a legitimate new connection
   * and user hasn't explicitly unselected it before
   */
  async smartAutoSelect(providerId: string, isLegitimateConnection: boolean = false): Promise<boolean> {
    if (!isLegitimateConnection) {
      return false; // Don't auto-select on polling/page refresh
    }

    const currentPreferences = await this.getProviderPreferences();
    
    // Don't auto-select if already selected
    if (currentPreferences.includes(providerId)) {
      return false;
    }

    // Check if user has any preferences at all (first-time user)
    const isFirstTimeUser = currentPreferences.length === 0;
    
    if (isFirstTimeUser) {
      // Auto-select for first-time users
      return await this.addProvider(providerId);
    }

    // For existing users, always auto-select legitimate new connections

    // Auto-select for legitimate new connections
    return await this.addProvider(providerId);
  }


  /**
   * Clear cache (force refresh from database)
   */
  clearCache(): void {
    this.cache = null;
    this.cacheTimestamp = 0;
    localStorage.removeItem('provider_preferences_cache');
  }

  /**
   * Get fallback preferences from localStorage or defaults
   */
  private getFallbackPreferences(): string[] {
    try {
      const cached = localStorage.getItem('provider_preferences_cache');
      if (cached) {
        const parsed = JSON.parse(cached);
        if (Array.isArray(parsed)) {
          return parsed;
        }
      }
      
      // Legacy fallback
      const legacy = localStorage.getItem('provider_preferences');
      if (legacy) {
        const parsed = JSON.parse(legacy);
        if (Array.isArray(parsed)) {
          return parsed;
        }
      }
    } catch (error) {
      console.error('Error parsing fallback preferences:', error);
    }
    
    // Default to empty array
    return [];
  }
}

// Export singleton instance
export const providerPreferencesService = new ProviderPreferencesService();
