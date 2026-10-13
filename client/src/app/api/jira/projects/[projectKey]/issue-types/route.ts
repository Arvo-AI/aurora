import { NextRequest } from 'next/server';
import { forwardAuthenticatedGet } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ projectKey: string }> },
) {
  const { projectKey } = await params;
  return forwardAuthenticatedGet(
    request,
    `/jira/projects/${encodeURIComponent(projectKey)}/issue-types`,
    'list Jira issue types',
  );
}
