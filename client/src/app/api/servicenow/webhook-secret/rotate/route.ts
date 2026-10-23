import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/servicenow/webhook-secret/rotate', 'servicenow/webhook-secret/rotate');
}
