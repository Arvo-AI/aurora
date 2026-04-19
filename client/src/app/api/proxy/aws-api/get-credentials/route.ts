import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/aws_api/get-credentials', 'aws-get-credentials');
}

export { handler as POST };
