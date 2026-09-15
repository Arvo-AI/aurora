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

// Default-ON flag (matches the backend's is_incident_prevention_enabled):
// only an explicit "false" disables it, so an unset env in dev keeps parity.
export const isIncidentPreventionEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION') !== 'false';
};

export const isCloudBeesEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_CLOUDBEES') === 'true';
};

export const isBitbucketOAuthEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_BITBUCKET_OAUTH') === 'true';
};

// Off unless explicitly enabled, mirroring the backend VISUALIZATION_ENABLED default.
export const isVisualizationEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_VISUALIZATION') === 'true';
};
