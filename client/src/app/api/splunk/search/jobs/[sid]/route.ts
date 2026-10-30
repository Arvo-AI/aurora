import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ sid: string }> }
) {
  const { sid } = await params;
  const jobPath = `/splunk/search/jobs/${encodeURIComponent(sid)}`;
  // results=true is the results endpoint. The job URL only returns status.
  const path = request.nextUrl.searchParams.get('results') === 'true'
    ? `${jobPath}/results`
    : jobPath;
  return forwardRequest(request, 'GET', path, 'splunk-job');
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ sid: string }> }
) {
  const { sid } = await params;
  return forwardRequest(
    request,
    'DELETE',
    `/splunk/search/jobs/${encodeURIComponent(sid)}`,
    'splunk-job',
    { passBody: false },
  );
}
