// ============================================================================
// Incident Feedback Service (Aurora Learn)
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
  feedback_id: string;
  feedback_type: FeedbackType;
  stored_for_learning: boolean;
}

export interface GetFeedbackResponse {
  feedback: IncidentFeedback | null;
}

/**
 * Submit feedback for an incident (thumbs up/down).
 */
export async function submitFeedback(
  incidentId: string,
  feedbackType: FeedbackType,
  comment?: string
): Promise<SubmitFeedbackResponse> {
  const response = await fetch(`/api/incidents/${incidentId}/feedback`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      feedback_type: feedbackType,
      comment: comment || undefined,
    }),
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Failed to submit feedback: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get existing feedback for an incident.
 */
export async function getFeedback(incidentId: string): Promise<IncidentFeedback | null> {
  const response = await fetch(`/api/incidents/${incidentId}/feedback`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Failed to get feedback: ${response.statusText}`);
  }

  const data: GetFeedbackResponse = await response.json();
  return data.feedback;
}

/**
 * Convenience object for importing all feedback functions.
 */
export const incidentFeedbackService = {
  submitFeedback,
  getFeedback,
};
