import { Project } from '../types';

// Cache configuration
const CACHE_DURATION = 5 * 60 * 1000; // 5 minutes
const CACHE_PREFIX = 'aurora_projects_';

interface CacheEntry {
  projects: Project[];
  timestamp: number;
}

export class ProjectCache {
  private static getKey(providerId: string): string {
    return `${CACHE_PREFIX}${providerId}`;
  }

  static get(providerId: string): Project[] | null {
    try {
      const cached = localStorage.getItem(this.getKey(providerId));
      if (!cached) return null;

      const entry: CacheEntry = JSON.parse(cached);
      const age = Date.now() - entry.timestamp;
      
      if (age > CACHE_DURATION) {
        this.invalidate(providerId);
        return null;
      }

      return entry.projects;
    } catch {
      return null;
    }
  }

  static set(providerId: string, projects: Project[]): void {
    try {
      const entry: CacheEntry = {
        projects,
        timestamp: Date.now()
      };
      localStorage.setItem(this.getKey(providerId), JSON.stringify(entry));
    } catch (error) {
      console.error(`[Cache] Failed to store ${providerId}:`, error);
    }
  }

  static invalidate(providerId: string): void {
    localStorage.removeItem(this.getKey(providerId));
  }

  static invalidateAll(): void {
    try {
      const keysToRemove: string[] = [];

      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.startsWith(CACHE_PREFIX)) {
          keysToRemove.push(key);
        }
      }

      keysToRemove.forEach(key => localStorage.removeItem(key));
    } catch (error) {
      console.error('[Cache] Failed to invalidate all project caches:', error);
    }
  }
}
