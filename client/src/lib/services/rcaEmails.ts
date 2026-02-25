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

import { getEnv } from '@/lib/env';

const API_BASE_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

/**
 * List all RCA notification emails for the user
 */
export async function listRCAEmails(userId: string): Promise<RCAEmailsResponse> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
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
export async function addRCAEmail(userId: string, email: string): Promise<void> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails/add`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
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
export async function verifyRCAEmail(userId: string, email: string, code: string): Promise<void> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails/verify`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
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
export async function resendVerificationCode(userId: string, email: string): Promise<void> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails/resend`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
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
export async function toggleRCAEmail(userId: string, emailId: number, isEnabled: boolean): Promise<void> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails/${emailId}/toggle`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
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
export async function removeRCAEmail(userId: string, emailId: number): Promise<void> {
  if (!userId) {
    throw new Error('User not authenticated');
  }

  const response = await fetch(`${API_BASE_URL}/api/rca-emails/${emailId}`, {
    method: 'DELETE',
    headers: {
      'Content-Type': 'application/json',
      'X-User-ID': userId,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Failed to remove email' }));
    throw new Error(error.error || 'Failed to remove email');
  }
}

