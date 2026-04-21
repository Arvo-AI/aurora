import { forwardAuthenticatedRequest } from '@/lib/backend-proxy';

export async function GET() {
  return forwardAuthenticatedRequest('/incidentio/alerts/webhook-url', 'incident-io/webhook-url');
}
