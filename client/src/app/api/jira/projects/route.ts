import { NextRequest } from 'next/server';
import { forwardAuthenticatedGet } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardAuthenticatedGet(request, '/jira/projects', 'list Jira projects');
}
