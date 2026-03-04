export interface JiraIssue {
  key: string;
  summary: string;
  status?: string;
  assignee?: string;
  priority?: string;
  labels?: string[];
  created?: string;
  updated?: string;
}

export interface JiraSearchResponse {
  total: number;
  count: number;
  issues: JiraIssue[];
}

const API_BASE = '/api/jira';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) return null;
  return JSON.parse(text) as T;
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T | null> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  if (!response.ok) {
    type ErrorBody = { error?: string; details?: string };
    const parsed = await parseJsonResponse<ErrorBody>(response).catch(() => null);
    const message = parsed?.error || parsed?.details || response.statusText || `Request failed with status ${response.status}`;
    throw new Error(message);
  }
  return parseJsonResponse<T>(response);
}

export const jiraService = {
  async searchIssues(jql: string, maxResults = 20): Promise<JiraSearchResponse | null> {
    return handleJsonFetch<JiraSearchResponse>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify({ jql, maxResults }),
    });
  },

  async getIssue(issueKey: string): Promise<Record<string, unknown> | null> {
    return handleJsonFetch<Record<string, unknown>>(`${API_BASE}/issue/${issueKey}`);
  },

  async createIssue(payload: { projectKey: string; summary: string; description?: string; issueType?: string; labels?: string[] }): Promise<Record<string, unknown> | null> {
    return handleJsonFetch<Record<string, unknown>>(`${API_BASE}/issue`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },
};
