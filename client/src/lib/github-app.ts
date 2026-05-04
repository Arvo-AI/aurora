import { getEnv } from '@/lib/env';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

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
  private static getAuthHeaders(userId: string) {
    return { 'X-User-ID': userId };
  }

  private static async request<T>(
    path: string,
    userId: string,
    options: { method?: string; errorMessage?: string } = {}
  ): Promise<T> {
    const { method, errorMessage = 'Request failed' } = options;
    const response = await fetch(`${BACKEND_URL}${path}`, {
      method,
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();

      try {
        const errorData = errorText ? JSON.parse(errorText) : null;
        if (typeof errorData?.error === 'string' && errorData.error.trim()) {
          throw new Error(errorData.error);
        }
      } catch (parseError) {
        if (parseError instanceof Error && parseError.message !== errorText) {
          throw parseError;
        }
      }

      throw new Error(errorText || errorMessage);
    }

    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  static async getInstallUrl(userId: string): Promise<string> {
    const data = await this.request<GitHubInstallUrlResponse>(
      '/github/app/install',
      userId,
      { errorMessage: 'Failed to fetch GitHub App install URL' }
    );

    if (!data?.install_url) {
      throw new Error('GitHub App install URL was not returned by the backend');
    }

    return data.install_url;
  }

  static async listInstallations(userId: string): Promise<GitHubInstallationsResponse> {
    return this.request<GitHubInstallationsResponse>(
      `/github/app/installations?user_id=${encodeURIComponent(userId)}`,
      userId,
      { errorMessage: 'Failed to fetch GitHub App installations' }
    );
  }

  static async unlinkInstallation(userId: string, installationId: number): Promise<void> {
    await this.request(
      `/github/app/installations/${encodeURIComponent(String(installationId))}?user_id=${encodeURIComponent(userId)}`,
      userId,
      { method: 'DELETE', errorMessage: 'Failed to unlink GitHub App installation' }
    );
  }
}
