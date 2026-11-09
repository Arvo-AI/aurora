import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/sandboxes', 'Failed to fetch sandboxes');
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/sandboxes', 'Failed to create sandbox');
}
