import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/victorops', 'victorops');
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/victorops', 'victorops');
}

export async function DELETE(request: NextRequest) {
  return forwardRequest(request, 'DELETE', '/victorops', 'victorops');
}
