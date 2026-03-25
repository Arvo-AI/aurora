// ============================================================================
// Shared API Client — single source of truth for all service fetch calls
//
// Every service in lib/services/* should use these helpers instead of raw
// fetch(). They provide retry, timeout, and consistent error handling.
// ============================================================================

import { fetchR } from '@/lib/query';

export interface ApiError extends Error {
  code?: string;
  status?: number;
}

export interface ApiRequestOptions extends RequestInit {
  timeout?: number;
  retries?: number;
  retryDelay?: number;
}

export function createApiError(
  message: string,
  code?: string,
  status?: number,
): ApiError {
  const error = new Error(message) as ApiError;
  error.code = code;
  error.status = status;
  return error;
}

export async function apiRequest<T>(
  url: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { timeout = 20_000, retries = 2, retryDelay = 1500, ...fetchOptions } = options;

  try {
    const response = await fetchR(url, {
      ...fetchOptions,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...fetchOptions.headers,
      },
      timeout,
      retries,
      retryDelay,
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw createApiError(
        data.error || `Request failed: ${response.statusText}`,
        data.error_code,
        response.status,
      );
    }

    const text = await response.text();
    if (!text) return {} as T;
    return JSON.parse(text) as T;
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      throw createApiError('Request timed out', 'TIMEOUT');
    }
    throw err;
  }
}

export function apiGet<T>(url: string, options?: ApiRequestOptions): Promise<T> {
  return apiRequest<T>(url, { ...options, method: 'GET' });
}

export function apiPost<T>(
  url: string,
  body?: unknown,
  options?: ApiRequestOptions,
): Promise<T> {
  return apiRequest<T>(url, {
    ...options,
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  });
}

export function apiPut<T>(
  url: string,
  body?: unknown,
  options?: ApiRequestOptions,
): Promise<T> {
  return apiRequest<T>(url, {
    ...options,
    method: 'PUT',
    body: body ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T>(
  url: string,
  options?: ApiRequestOptions,
): Promise<T> {
  return apiRequest<T>(url, { ...options, method: 'DELETE' });
}
