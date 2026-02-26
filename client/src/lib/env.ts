type EnvKey =
  | 'NEXT_PUBLIC_BACKEND_URL'
  | 'NEXT_PUBLIC_WEBSOCKET_URL'
  | 'NEXT_PUBLIC_ENABLE_OVH'
  | 'NEXT_PUBLIC_ENABLE_SLACK'
  | 'NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH'
  | 'NEXT_PUBLIC_ENABLE_CONFLUENCE'
  | 'NEXT_PUBLIC_ENABLE_SCALEWAY'
  | 'NEXT_PUBLIC_ENABLE_BITBUCKET'
  | 'NEXT_PUBLIC_ENABLE_DYNATRACE'
  | 'NEXT_PUBLIC_ENABLE_BIGPANDA';

declare global {
  interface Window {
    __ENV?: Record<string, string>;
  }
}

/**
 * Read a NEXT_PUBLIC_* env var at runtime.
 * In the browser, prefers the value injected by /env-config.js (set at container startup).
 * Falls back to the build-time value from process.env for dev mode / SSR.
 */
export function getEnv(key: EnvKey): string | undefined {
  if (typeof window !== 'undefined' && window.__ENV?.[key]) {
    return window.__ENV[key];
  }
  return process.env[key];
}
