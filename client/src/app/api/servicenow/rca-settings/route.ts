import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/servicenow/rca-settings', 'servicenow/rca-settings');
}

export async function PUT(request: NextRequest) {
  return forwardRequest(request, 'PUT', '/servicenow/rca-settings', 'servicenow/rca-settings');
}
