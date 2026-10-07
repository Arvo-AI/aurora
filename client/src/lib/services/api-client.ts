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

/**
 * Pull the backend's own error text off a thrown error so callers can show the
 * actual reason instead of a generic message.
 *
 * `apiRequest` copies the response's `data.error` into `Error.message`, so a
 * backend 4xx with a JSON body already carries something worth displaying (e.g.
 * "channel_ids must all be non-empty strings"). But it also throws for
 * cases with no useful text — a transport failure, or a non-JSON error body
 * where the message degrades to "Request failed: <statusText>". Those aren't
 * actionable, so prefer the caller's copy for them.
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;

  const message = error.message.trim();

  // No message at all — nothing to surface.
  if (!message) return fallback;

  // Placeholder built by createApiError when the body had no `error` field.
  if (message.startsWith('Request failed:')) return fallback;

  // Network-level failure, not something the backend told us.
  if (message === 'Failed to fetch' || message === 'Load failed') return fallback;

  return message;
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
