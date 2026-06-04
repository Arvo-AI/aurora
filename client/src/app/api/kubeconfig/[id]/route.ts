import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function DELETE(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const { id } = params
  return forwardRequest(request, 'DELETE', `/api/kubeconfig/${id}`, 'kubeconfig delete')
}
