export type GitHubAccountType = 'User' | 'Organization';

interface GitHubInstallUrlResponse {
  install_url: string;
}

export interface GitHubInstallation {
  installation_id: number;
  account_login: string;
  account_type: GitHubAccountType;
  account_id: number;
  repository_selection: 'all' | 'selected';
  suspended_at: string | null;
  permissions_pending_update: boolean;
}

export interface GitHubInstallationsResponse {
  installations: GitHubInstallation[];
}

export class GitHubAppService {
  private static async request<T>(
    path: string,
    options: { method?: string; errorMessage?: string } = {}
  ): Promise<T> {
    const { method, errorMessage = 'Request failed' } = options;
    const response = await fetch(`/api/proxy/github${path}`, { method });

    if (!response.ok) {
      const errorText = await response.text();

      let parsedMessage: string | null = null;
      if (errorText) {
        try {
          const errorData = JSON.parse(errorText);
          if (typeof errorData?.error === 'string' && errorData.error.trim()) {
            parsedMessage = errorData.error;
          }
        } catch {
          // Malformed JSON body — fall through to errorText/errorMessage.
        }
      }
      throw new Error(parsedMessage || errorText || errorMessage);
    }

    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  static async getInstallUrl(): Promise<string> {
    const data = await this.request<GitHubInstallUrlResponse>(
      '/app/install',
      { errorMessage: 'Failed to fetch GitHub App install URL' }
    );

    if (!data?.install_url) {
      throw new Error('GitHub App install URL was not returned by the backend');
    }

    return data.install_url;
  }

  static async listInstallations(): Promise<GitHubInstallationsResponse> {
    return this.request<GitHubInstallationsResponse>(
      '/app/installations',
      { errorMessage: 'Failed to fetch GitHub App installations' }
    );
  }

  static async unlinkInstallation(installationId: number): Promise<void> {
    await this.request(
      `/app/installations/${encodeURIComponent(String(installationId))}`,
      { method: 'DELETE', errorMessage: 'Failed to unlink GitHub App installation' }
    );
  }
}
