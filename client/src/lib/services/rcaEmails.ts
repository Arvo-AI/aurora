/**
 * RCA Email Management API Client
 * Handles additional email addresses for RCA notifications
 */

export interface RCAEmail {
  id: number;
  email: string;
  is_verified: boolean;
  is_enabled: boolean;
  created_at: string;
  verified_at?: string;
}

export interface RCAEmailsResponse {
  primary_email: string;
  additional_emails: RCAEmail[];
}

/**
 * List all RCA notification emails for the user
 */
export async function listRCAEmails(): Promise<RCAEmailsResponse> {
  const response = await fetch('/api/proxy/rca-emails', {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to fetch emails' }));
    throw new Error(error.error || 'Failed to fetch emails');
  }

  return response.json();
}

/**
 * Add a new email address and send verification code
 */
export async function addRCAEmail(email: string): Promise<void> {
  const response = await fetch('/api/proxy/rca-emails/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to add email' }));
    throw new Error(error.error || 'Failed to add email');
  }
}

/**
 * Verify an email address with the provided code
 */
export async function verifyRCAEmail(email: string, code: string): Promise<void> {
  const response = await fetch('/api/proxy/rca-emails/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, code }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to verify email' }));
    throw new Error(error.error || 'Failed to verify email');
  }
}

/**
 * Resend verification code to an email address
 */
export async function resendVerificationCode(email: string): Promise<void> {
  const response = await fetch('/api/proxy/rca-emails/resend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to resend code' }));
    throw new Error(error.error || 'Failed to resend code');
  }
}

/**
 * Toggle an email address enabled/disabled
 */
export async function toggleRCAEmail(emailId: number, isEnabled: boolean): Promise<void> {
  const response = await fetch(`/api/proxy/rca-emails/${emailId}/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_enabled: isEnabled }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to toggle email' }));
    throw new Error(error.error || 'Failed to toggle email');
  }
}

/**
 * Remove an email address
 */
export async function removeRCAEmail(emailId: number): Promise<void> {
  const response = await fetch(`/api/proxy/rca-emails/${emailId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to remove email' }));
    throw new Error(error.error || 'Failed to remove email');
  }
}
