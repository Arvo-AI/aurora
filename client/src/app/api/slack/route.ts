import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/slack', 'slack');
}

async function deleteHandler(request: NextRequest) {
  return forwardRequest(request, 'DELETE', '/slack', 'slack', { passBody: false });
}

export { handler as GET, handler as POST, deleteHandler as DELETE };
