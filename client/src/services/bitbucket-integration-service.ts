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
  private static async request<T>(
    path: string,
    options: { method?: string; body?: object; errorMessage?: string | null } = {}
  ): Promise<T> {
    const { method, body, errorMessage } = options;
    const headers: Record<string, string> = {};
    if (body) headers['Content-Type'] = 'application/json';

    const response = await fetch(`/api/proxy/bitbucket${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      if (errorMessage === null) return null as T;
      const errorText = await response.text();
      let message = errorText;
      try {
        const json = JSON.parse(errorText);
        message = json.message || json.error || errorText;
      } catch {
        // plain text error, use as-is
      }
      throw new Error(message || errorMessage || 'Request failed');
    }

    const contentType = response.headers.get('content-type');
    if (contentType?.includes('application/json')) {
      return response.json();
    }
    return undefined as T;
  }

  static async checkStatus(): Promise<StatusResponse> {
    return this.request<StatusResponse>(
      '/status', { errorMessage: null }
    ).then(data => data ?? { connected: false });
  }

  static async initiateOAuth(): Promise<string> {
    const data = await this.request<{ oauth_url?: string }>(
      '/login',
      { method: 'POST', body: {}, errorMessage: 'Failed to initiate Bitbucket OAuth' }
    );
    if (!data?.oauth_url) {
      throw new Error('Bitbucket OAuth URL was not returned by the server');
    }
    return data.oauth_url;
  }

  static async connectWithApiToken(email: string, apiToken: string): Promise<{ success: boolean; message: string }> {
    return this.request(
      '/login',
      { method: 'POST', body: { api_token: apiToken, email }, errorMessage: 'Failed to connect with API token' }
    );
  }

  static async disconnect(): Promise<void> {
    await this.request(
      '/disconnect',
      { method: 'POST', errorMessage: 'Failed to disconnect Bitbucket' }
    );
  }

  static async getWorkspaces(): Promise<WorkspacesResponse> {
    return this.request<WorkspacesResponse>('/workspaces', { errorMessage: 'Failed to fetch workspaces' });
  }

  static async getProjects(workspace: string): Promise<{ projects: Record<string, unknown>[] }> {
    return this.request(
      `/projects/${encodeURIComponent(workspace)}`,
      { errorMessage: 'Failed to fetch projects' }
    );
  }

  static async getRepos(workspace: string, project?: string): Promise<ReposResponse> {
    const base = `/repos/${encodeURIComponent(workspace)}`;
    const path = project ? `${base}?project=${encodeURIComponent(project)}` : base;
    return this.request<ReposResponse>(path, { errorMessage: 'Failed to fetch repositories' });
  }

  static async getBranches(workspace: string, repoSlug: string): Promise<BranchesResponse> {
    return this.request<BranchesResponse>(
      `/branches/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`,
      { errorMessage: 'Failed to fetch branches' }
    );
  }

  static async getPullRequests(workspace: string, repoSlug: string, state?: string): Promise<PullRequestsResponse> {
    const base = `/pull-requests/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`;
    const path = state ? `${base}?state=${encodeURIComponent(state)}` : base;
    return this.request<PullRequestsResponse>(path, { errorMessage: 'Failed to fetch pull requests' });
  }

  static async getIssues(workspace: string, repoSlug: string): Promise<IssuesResponse> {
    return this.request<IssuesResponse>(
      `/issues/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`,
      { errorMessage: 'Failed to fetch issues' }
    );
  }

  static async loadWorkspaceSelection(): Promise<WorkspaceSelectionResponse | null> {
    return this.request<WorkspaceSelectionResponse>('/workspace-selection', { errorMessage: null });
  }

  static async saveWorkspaceSelection(data: { workspace: string; repository: Repo; branch: string }): Promise<{ message: string }> {
    return this.request(
      '/workspace-selection',
      { method: 'POST', body: data, errorMessage: 'Failed to save workspace selection' }
    );
  }

  static async clearWorkspaceSelection(): Promise<void> {
    await this.request(
      '/workspace-selection',
      { method: 'DELETE', errorMessage: 'Failed to clear workspace selection' }
    );
  }
}
