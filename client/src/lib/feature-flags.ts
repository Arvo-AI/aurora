/**
 * Feature flags for toggling functionality.
 * Uses NEXT_PUBLIC_ENABLE_* variables shared with backend for single source of truth.
 */

import { getEnv } from '@/lib/env';

export const isPagerDutyOAuthEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH') === 'true';
};

export const isOvhEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_OVH') === 'true';
};

export const isSlackEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_SLACK') === 'true';
};

export const isConfluenceEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_CONFLUENCE') === 'true';
};

export const isScalewayEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_SCALEWAY') === 'true';
};

export const isBitbucketEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_BITBUCKET') === 'true';
};  
  
export const isDynatraceEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_DYNATRACE') === 'true';
};

export const isThousandEyesEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_THOUSANDEYES') === 'true';
};