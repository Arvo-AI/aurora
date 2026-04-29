import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/settings/multi-agent-config', 'settings/multi-agent-config');
}

export async function PUT(request: NextRequest) {
  return forwardRequest(request, 'PUT', '/api/settings/multi-agent-config', 'settings/multi-agent-config');
}
