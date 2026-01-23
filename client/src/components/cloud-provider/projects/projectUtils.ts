import { Project } from '../types';
import { ProjectCache } from './projectCache';

// Merge cached/API projects with current user selections
const mergeProjectsWithUserSelections = (newProjects: Project[], currentProjects: Project[]): Project[] => {
  return newProjects.map(newProject => {
    // Find if this project exists in current state
    const currentProject = currentProjects.find(cp => cp.projectId === newProject.projectId);

    if (currentProject) {
      // Preserve user's enabled/disabled choice, but use new API data for hasPermission and isRootProject
      return {
        ...newProject,
        enabled: currentProject.enabled, // Keep user's selection
        hasPermission: newProject.hasPermission, // Use fresh permission data from API
        isRootProject: newProject.isRootProject // Use fresh root project status from API
      };
    } else {
      // New project, use default from API/cache
      return newProject;
    }
  });
};

const endpoints = {
  gcp: '/api/gcp-projects',
  azure: '/api/azure-subscriptions',
  ovh: '/api/ovh-projects',
  scaleway: '/api/scaleway-projects',
  aws: '/api/aws-projects'
};

const getUserId = async (): Promise<string> => {
  try {
    const response = await fetch('/api/getUserId');
    const data = await response.json();
    return data.userId || '';
  } catch {
    return '';
  }
};

// Fetch projects with caching
export const fetchProjects = async (providerId: string, forceRefresh = false, currentProjects: Project[] = []): Promise<Project[]> => {
  // These providers don't have projects - skip
  if (providerId === 'grafana' || providerId === 'datadog' || providerId === 'netdata' || providerId === 'tailscale' || providerId === 'slack') {
    return [];
  }
  
  // Check cache first (unless force refresh)
  if (!forceRefresh) {
    const cached = ProjectCache.get(providerId);
    if (cached) {
      return mergeProjectsWithUserSelections(cached, currentProjects);
    }
  }
  const endpoint = endpoints[providerId as keyof typeof endpoints];
  if (!endpoint) {
    console.error(`[fetchProjects] No endpoint for provider: ${providerId}`);
    return [];
  }
  
  try {
    // Get user ID for authentication
    const userId = await getUserId();
    const headers: HeadersInit = {};
    if (userId) {
      headers['X-User-ID'] = userId;
    }
    
    const res = await fetch(endpoint, { headers });
    
    if (!res.ok) {
      const errorText = await res.text();

      if (res.status === 401 && errorText.toLowerCase().includes('not found')) {
        console.warn(`[fetchProjects] Projects not found for ${providerId}:`, errorText);
        const notFoundError = new Error(errorText || `No ${providerId} projects found.`) as Error & {
          status?: number;
          code?: string;
        };
        notFoundError.status = 401;
        notFoundError.code = 'PROJECT_NOT_FOUND';
        throw notFoundError;
      }

      console.error(`[fetchProjects] Error response for ${providerId}:`, errorText);
      const fetchError = new Error(`Failed to fetch ${providerId} projects: ${res.status} - ${errorText}`) as Error & {
        status?: number;
      };
      fetchError.status = res.status;
      throw fetchError;
    }
    
    const data = await res.json();
    
    const rawProjects = (Array.isArray(data.projects) ? data.projects : []) as Array<
      Partial<Project> & { id?: string; saEnabled?: boolean; hasPermission?: boolean; isRootProject?: boolean }
    >;

    const apiProjects: Project[] = rawProjects
      .map<Project | null>(p => {
        const projectId = p.projectId ?? p.id;
        if (!projectId) {
          return null;
        }

        return {
          projectId,
          name: p.name ?? projectId,
          enabled: p.enabled ?? p.saEnabled ?? false,
          hasPermission: p.hasPermission !== false, // Default to true if not specified
          isRootProject: p.isRootProject ?? false // Include root project status
        };
      })
      .filter((project): project is Project => project !== null);
    
    // Merge API data with current user selections
    const mergedProjects = mergeProjectsWithUserSelections(apiProjects, currentProjects);
    
    // Cache the merged results
    ProjectCache.set(providerId, mergedProjects);
    
    return mergedProjects;
  } catch (error) {
    console.error(`[fetchProjects] Exception for ${providerId}:`, error);
    throw error;
  }
};

export const saveProjects = async (providerId: string, projects: Project[]): Promise<void> => {
  const userId = await getUserId();
  const headers: HeadersInit = { 'Content-Type': 'application/json' };
  if (userId) {
    headers['X-User-ID'] = userId;
  }
  
  const res = await fetch(endpoints[providerId as keyof typeof endpoints], {
    method: 'POST',
    headers,
    body: JSON.stringify({ projects: projects.map(p => ({ projectId: p.projectId, enabled: p.enabled })) })
  });
  if (!res.ok) throw new Error(`Failed to save ${providerId} projects`);
  
  // Update cache after successful save
  ProjectCache.set(providerId, projects);
};

// Export cache utilities for external use
export { ProjectCache };
