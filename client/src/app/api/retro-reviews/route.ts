import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/retro-reviews', 'retro-reviews-list');
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/retro-reviews', 'retro-reviews-create');
}
