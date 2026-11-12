import { NextRequest } from 'next/server';
import { forwardAuthenticatedGet } from '@/lib/backend-proxy';

/**
 * GET API route to fetch Security Hub findings for the authenticated user.
 * Proxies the request to the backend while forwarding query parameters.
 */
export async function GET(request: NextRequest) {
  return forwardAuthenticatedGet(request, '/securityhub/findings', 'Security Hub findings');
}
