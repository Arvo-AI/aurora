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

export const isSharePointEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_SHAREPOINT') === 'true';
};

export const isJiraEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_JIRA') === 'true';
};

export const isSpinnakerEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_SPINNAKER') === 'true';
};

export const isNotionEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_NOTION') === 'true';
};
