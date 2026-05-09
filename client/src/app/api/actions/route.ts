import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/actions', 'actions-list');
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/actions', 'actions-create');
}
