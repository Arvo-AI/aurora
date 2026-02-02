// ============================================================================
// Shared API Client
// ============================================================================

export interface ApiError extends Error {
  code?: string;
  status?: number;
}

export interface ApiRequestOptions extends RequestInit {
  timeout?: number;
}

/**
 * Create an API error with optional error code and status.
 */
export function createApiError(
  message: string,
  code?: string,
  status?: number
): ApiError {
  const error = new Error(message) as ApiError;
  error.code = code;
  error.status = status;
  return error;
}

/**
 * Make an authenticated API request with standard error handling.
 */
export async function apiRequest<T>(
  url: string,
  options: ApiRequestOptions = {}
): Promise<T> {
  const { timeout = 30000, ...fetchOptions } = options;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...fetchOptions,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...fetchOptions.headers,
      },
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw createApiError(
        data.error || `Request failed: ${response.statusText}`,
        data.error_code,
        response.status
      );
    }

    return response.json();
  } catch (err) {
    clearTimeout(timeoutId);

    if (err instanceof Error && err.name === 'AbortError') {
      throw createApiError('Request timed out', 'TIMEOUT');
    }

    throw err;
  }
}

/**
 * Make a GET request.
 */
export function apiGet<T>(url: string, options?: ApiRequestOptions): Promise<T> {
  return apiRequest<T>(url, { ...options, method: 'GET' });
}

/**
 * Make a POST request.
 */
export function apiPost<T>(
  url: string,
  body?: unknown,
  options?: ApiRequestOptions
): Promise<T> {
  return apiRequest<T>(url, {
    ...options,
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  });
}

/**
 * Make a PUT request.
 */
export function apiPut<T>(
  url: string,
  body?: unknown,
  options?: ApiRequestOptions
): Promise<T> {
  return apiRequest<T>(url, {
    ...options,
    method: 'PUT',
    body: body ? JSON.stringify(body) : undefined,
  });
}
