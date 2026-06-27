import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// GET /api/artifacts            → list summaries
// GET /api/artifacts?title=...  → single artifact by exact title
export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/artifacts', 'Failed to fetch artifacts');
}

// POST /api/artifacts → create (or replace by title)
export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/artifacts', 'Failed to create artifact');
}
