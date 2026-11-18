import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;
  return forwardRequest(request, 'GET', `/api/backtest/reviews/${encodeURIComponent(jobId)}`, 'Failed to fetch backtest review');
}
