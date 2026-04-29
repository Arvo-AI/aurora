import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ role: string }> },
) {
  const { role } = await params;
  return forwardRequest(
    request,
    'DELETE',
    `/api/settings/model-roles/${role}`,
    `settings/model-roles/${role}`,
  );
}
