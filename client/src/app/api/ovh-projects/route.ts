import { NextRequest, NextResponse } from 'next/server';
import { auth } from '@/auth';

// Backend base URL (matches usage in other proxy routes)
const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const session = await auth();

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    const userId = session.userId;

    const response = await fetch(`${API_BASE_URL}/ovh_api/ovh/projects`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-User-ID': userId,
      },
    });

    if (!response.ok) {
      let errorData;
      try {
        const jsonData = await response.json();
        errorData = jsonData.error || jsonData.message || 'Failed to fetch OVH projects';
      } catch {
        errorData = await response.text() || 'Failed to fetch OVH projects';
      }
      console.error('[OVH Projects API] Backend error:', errorData);
      return NextResponse.json(
        { error: errorData },
        { status: response.status }
      );
    }

    const data = await response.json();
    
    // Transform projects to standard format
    const projects = (data.projects || []).map((project: any) => ({
      projectId: project.projectId,
      name: project.projectName || project.projectId,
      enabled: project.enabled ?? true,
      hasPermission: project.status === 'ok' || project.status === 'unknown',
      isRootProject: project.isRootProject ?? false,
    }));

    return NextResponse.json({ projects, root_project: data.root_project });
  } catch (error: any) {
    console.error('[OVH Projects API] Error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const session = await auth();

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    const userId = session.userId;
    const body = await request.json();

    const response = await fetch(`${API_BASE_URL}/ovh_api/ovh/projects`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-ID': userId,
      },
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorData = await response.text();
      console.error('[OVH Projects API] POST error:', errorData);
      return NextResponse.json(
        { error: errorData || 'Failed to update OVH projects' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: any) {
    console.error('[OVH Projects API] POST Error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function OPTIONS(request: NextRequest) {
  return new NextResponse(null, {
    status: 200,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    },
  });
}
