/**
 * Feature flags for toggling functionality.
 * Uses NEXT_PUBLIC_ENABLE_* variables shared with backend for single source of truth.
 */

import { getEnv } from '@/lib/env';

// Default-ON flag (matches the backend's is_incident_prevention_enabled):
// only an explicit "false" disables it, so an unset env in dev keeps parity.
export const isIncidentPreventionEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION') !== 'false';
};

// Off unless explicitly enabled, mirroring the backend VISUALIZATION_ENABLED default.
export const isVisualizationEnabled = () => {
  return getEnv('NEXT_PUBLIC_ENABLE_VISUALIZATION') === 'true';
};
