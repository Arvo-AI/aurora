import { forwardAuthenticatedRequest } from '@/lib/backend-proxy';

export async function GET() {
  return forwardAuthenticatedRequest('/incidentio/status', 'incident-io/status');
}
