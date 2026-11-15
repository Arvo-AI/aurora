import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/elastic/disconnect', 'elastic/disconnect');
}

export async function DELETE(request: NextRequest) {
  return forwardRequest(request, 'DELETE', '/elastic/disconnect', 'elastic/disconnect');
}
