import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Image bytes for thumbnails (backend only serves png/jpeg/gif/webp, with nosniff).
export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string; artifactId: string }> }) {
  const { id, artifactId } = await params;
  return forwardRequest(
    request, 'GET',
    `/api/sandboxes/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifactId)}/raw`,
    'Failed to fetch image',
  );
}
