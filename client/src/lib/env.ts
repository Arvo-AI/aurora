type EnvKey =
  | 'NEXT_PUBLIC_BACKEND_URL'
  | 'NEXT_PUBLIC_GITHUB_APP_SLUG'
  | 'NEXT_PUBLIC_WEBSOCKET_URL'
  | 'NEXT_PUBLIC_ENABLE_OVH'
  | 'NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH'
  | 'NEXT_PUBLIC_ENABLE_SHAREPOINT'
  | 'NEXT_PUBLIC_ENABLE_JIRA'
  | 'NEXT_PUBLIC_ENABLE_NOTION'
  | 'NEXT_PUBLIC_ENABLE_SPINNAKER'
  | 'NEXT_PUBLIC_ENABLE_CLOUDBEES'
  | 'NEXT_PUBLIC_ENABLE_BITBUCKET_OAUTH'
  | 'AURORA_ENV';

declare global {
  interface Window {
    __ENV?: Record<string, string>;
  }
}

/**
 * Read an env var at runtime.
 * In the browser, prefers the value injected by /env-config.js (set at container startup).
 * Falls back to the build-time value from process.env for dev mode / SSR.
 */
export function getEnv(key: EnvKey): string | undefined {
  if (typeof window !== 'undefined' && window.__ENV?.[key]) {
    return window.__ENV[key];
  }
  return process.env[key];
}
