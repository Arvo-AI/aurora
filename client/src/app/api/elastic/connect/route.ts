import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  // Connect validates against Elasticsearch (with retries) and probes Kibana;
  // give it more than the 30s default so a slow endpoint cannot 504 after the
  // backend has already stored the connection.
  return forwardRequest(request, 'POST', '/elastic/connect', 'elastic/connect', { timeoutMs: 60_000 });
}
