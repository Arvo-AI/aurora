import { getEnv } from '@/lib/env';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

// ----- Response types -----

export interface Workspace {
  slug: string;
  name: string;
  uuid: string;
}

export interface Repo {
  slug: string;
  name: string;
  full_name: string;
  is_private: boolean;
  description?: string;
  mainbranch?: { name: string };
}

export interface Branch {
  name: string;
}

export interface StatusResponse {
  connected: boolean;
  display_name?: string;
  username?: string;
  auth_type?: string;
}

interface WorkspacesResponse {
  workspaces: Workspace[];
}

interface ReposResponse {
  repositories: Repo[];
}

interface BranchesResponse {
  branches: Branch[];
}

interface PullRequestsResponse {
  pull_requests: Record<string, unknown>[];
}

interface IssuesResponse {
  issues: Record<string, unknown>[];
}

export interface WorkspaceSelectionResponse {
  workspace?: string;
  repository?: string | { slug: string; name: string };
  branch?: string | { name: string };
}

// ----- Service -----

export class BitbucketIntegrationService {
  private static getAuthHeaders(userId: string) {
    return { 'X-User-ID': userId };
  }

  /**
   * Shared fetch helper that handles auth headers, error responses, and JSON parsing.
   * Pass `errorMessage: null` to return null on non-OK responses instead of throwing.
   */
  private static async request<T>(
    path: string,
    userId: string,
    options: { method?: string; body?: object; errorMessage?: string | null } = {}
  ): Promise<T> {
    const { method, body, errorMessage } = options;
    const headers: Record<string, string> = { ...this.getAuthHeaders(userId) };
    if (body) headers['Content-Type'] = 'application/json';

    const response = await fetch(`${BACKEND_URL}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      if (errorMessage === null) return null as T;
      const errorText = await response.text();
      throw new Error(errorText || errorMessage || 'Request failed');
    }

    const contentType = response.headers.get('content-type');
    if (contentType?.includes('application/json')) {
      return response.json();
    }
    return undefined as T;
  }

  static async checkStatus(userId: string): Promise<StatusResponse> {
    return this.request<StatusResponse>(
      '/bitbucket/status', userId, { errorMessage: null }
    ).then(data => data ?? { connected: false });
  }

  static async initiateOAuth(userId: string): Promise<string> {
    const data = await this.request<{ oauth_url?: string }>(
      '/bitbucket/login', userId,
      { method: 'POST', body: {}, errorMessage: 'Failed to initiate Bitbucket OAuth' }
    );
    if (!data?.oauth_url) {
      throw new Error('Bitbucket OAuth URL was not returned by the server');
    }
    return data.oauth_url;
  }

  static async connectWithApiToken(userId: string, email: string, apiToken: string): Promise<{ success: boolean; message: string }> {
    return this.request(
      '/bitbucket/login', userId,
      { method: 'POST', body: { api_token: apiToken, email }, errorMessage: 'Failed to connect with API token' }
    );
  }

  static async disconnect(userId: string): Promise<void> {
    await this.request(
      '/bitbucket/disconnect', userId,
      { method: 'POST', errorMessage: 'Failed to disconnect Bitbucket' }
    );
  }

  static async getWorkspaces(userId: string): Promise<WorkspacesResponse> {
    return this.request<WorkspacesResponse>('/bitbucket/workspaces', userId, { errorMessage: 'Failed to fetch workspaces' });
  }

  static async getProjects(userId: string, workspace: string): Promise<{ projects: Record<string, unknown>[] }> {
    return this.request(
      `/bitbucket/projects/${encodeURIComponent(workspace)}`, userId,
      { errorMessage: 'Failed to fetch projects' }
    );
  }

  static async getRepos(userId: string, workspace: string, project?: string): Promise<ReposResponse> {
    const path = `/bitbucket/repos/${encodeURIComponent(workspace)}${project ? `?project=${encodeURIComponent(project)}` : ''}`;
    return this.request<ReposResponse>(path, userId, { errorMessage: 'Failed to fetch repositories' });
  }

  static async getBranches(userId: string, workspace: string, repoSlug: string): Promise<BranchesResponse> {
    return this.request<BranchesResponse>(
      `/bitbucket/branches/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`, userId,
      { errorMessage: 'Failed to fetch branches' }
    );
  }

  static async getPullRequests(userId: string, workspace: string, repoSlug: string, state?: string): Promise<PullRequestsResponse> {
    const path = `/bitbucket/pull-requests/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}${state ? `?state=${encodeURIComponent(state)}` : ''}`;
    return this.request<PullRequestsResponse>(path, userId, { errorMessage: 'Failed to fetch pull requests' });
  }

  static async getIssues(userId: string, workspace: string, repoSlug: string): Promise<IssuesResponse> {
    return this.request<IssuesResponse>(
      `/bitbucket/issues/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`, userId,
      { errorMessage: 'Failed to fetch issues' }
    );
  }

  static async loadWorkspaceSelection(userId: string): Promise<WorkspaceSelectionResponse | null> {
    return this.request<WorkspaceSelectionResponse>('/bitbucket/workspace-selection', userId, { errorMessage: null });
  }

  static async saveWorkspaceSelection(userId: string, data: { workspace: string; repository: Repo; branch: string }): Promise<{ message: string }> {
    return this.request(
      '/bitbucket/workspace-selection', userId,
      { method: 'POST', body: data, errorMessage: 'Failed to save workspace selection' }
    );
  }

  static async clearWorkspaceSelection(userId: string): Promise<void> {
    await this.request(
      '/bitbucket/workspace-selection', userId,
      { method: 'DELETE', errorMessage: 'Failed to clear workspace selection' }
    );
  }
}
