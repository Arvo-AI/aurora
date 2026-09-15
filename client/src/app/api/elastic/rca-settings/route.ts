import { NextRequest } from 'next/server';
import { forwardAuthenticatedGet, forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardAuthenticatedGet(request, '/elastic/rca-settings', 'elastic/rca-settings');
}

export async function PUT(request: NextRequest) {
  return forwardRequest(request, 'PUT', '/elastic/rca-settings', 'elastic/rca-settings');
}
