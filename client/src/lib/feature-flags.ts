/**
 * Feature flags for toggling functionality.
 * Uses NEXT_PUBLIC_ENABLE_* variables shared with backend for single source of truth.
 */

export const isPagerDutyOAuthEnabled = () => {
  return process.env.NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH === 'true';
};

export const isOvhEnabled = () => {
  return process.env.NEXT_PUBLIC_ENABLE_OVH === 'true';
};

export const isSlackEnabled = () => {
  return process.env.NEXT_PUBLIC_ENABLE_SLACK === 'true';
};

export const isConfluenceEnabled = () => {
  return process.env.NEXT_PUBLIC_ENABLE_CONFLUENCE === 'true';
};
