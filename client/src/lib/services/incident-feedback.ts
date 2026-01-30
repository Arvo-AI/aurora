// ============================================================================
// Aurora Learn Service
// - Incident feedback (thumbs up/down)
// - Aurora Learn settings (enable/disable)
// ============================================================================

import { apiGet, apiPost, apiPut } from './api-client';

// ============================================================================
// Types
// ============================================================================

export type FeedbackType = 'helpful' | 'not_helpful';

export interface IncidentFeedback {
  id: string;
  feedbackType: FeedbackType;
  comment?: string;
  createdAt: string;
}

export interface SubmitFeedbackResponse {
  success: boolean;
  feedbackId: string;
  feedbackType: FeedbackType;
  storedForLearning: boolean;
  createdAt: string;
}

export interface AuroraLearnSetting {
  enabled: boolean;
}

interface GetFeedbackResponse {
  feedback: IncidentFeedback | null;
}

interface SetAuroraLearnResponse {
  success: boolean;
  enabled: boolean;
}

// ============================================================================
// Incident Feedback
// ============================================================================

/**
 * Submit feedback for an incident (thumbs up/down).
 */
export async function submitFeedback(
  incidentId: string,
  feedbackType: FeedbackType,
  comment?: string
): Promise<SubmitFeedbackResponse> {
  return apiPost<SubmitFeedbackResponse>(
    `/api/incidents/${incidentId}/feedback`,
    { feedback_type: feedbackType, comment: comment || undefined }
  );
}

/**
 * Get existing feedback for an incident.
 */
export async function getFeedback(incidentId: string): Promise<IncidentFeedback | null> {
  const data = await apiGet<GetFeedbackResponse>(`/api/incidents/${incidentId}/feedback`);
  return data.feedback;
}

// ============================================================================
// Aurora Learn Settings
// ============================================================================

/**
 * Get the Aurora Learn setting for the current user.
 * Defaults to true if not set.
 */
export async function getAuroraLearnSetting(): Promise<AuroraLearnSetting> {
  return apiGet<AuroraLearnSetting>('/api/user/preferences/aurora-learn');
}

/**
 * Set the Aurora Learn setting for the current user.
 */
export async function setAuroraLearnSetting(
  enabled: boolean
): Promise<SetAuroraLearnResponse> {
  return apiPut<SetAuroraLearnResponse>('/api/user/preferences/aurora-learn', { enabled });
}

// ============================================================================
// Service Objects (for convenience imports)
// ============================================================================

export const incidentFeedbackService = {
  submitFeedback,
  getFeedback,
};

export const userPreferencesService = {
  getAuroraLearnSetting,
  setAuroraLearnSetting,
};
