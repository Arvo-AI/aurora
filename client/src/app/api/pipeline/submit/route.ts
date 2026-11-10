import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// dbt manifests can be tens of MB; give the upload more time than the 30s default.
export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/pipeline/submit', 'pipeline-submit', { timeoutMs: 120_000 });
}
