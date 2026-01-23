import { NextRequest, NextResponse } from 'next/server';
import { auth } from '@/auth';

const backendUrl = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!backendUrl) {
      return NextResponse.json({ error: 'Backend URL not configured' }, { status: 500 });
    }

    const session = await auth();
    
    if (!session?.userId) {
      return new NextResponse('Unauthorized', { status: 401 });
    }

    const userId = session.userId;

    // Get query parameters for filtering
    const searchParams = request.nextUrl.searchParams;
    const costThreshold = searchParams.get('costThreshold') || '0.01';
    const serviceFilter = searchParams.get('serviceFilter') || '';
    const aggregateByDay = searchParams.get('aggregateByDay') !== 'false';
    
    // Build the query string
    const queryParams = new URLSearchParams({
      user_id: userId,
      cost_threshold: costThreshold,
      aggregate_by_day: aggregateByDay.toString()
    });
    
    if (serviceFilter) {
      queryParams.append('service_filter', serviceFilter);
    }

    const response = await fetch(`${backendUrl}/aws_api/billing?${queryParams.toString()}`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-User-ID': userId,
      },
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch AWS billing data: ${response.statusText}`);
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: any) {
    console.error('Error fetching AWS billing data:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
} 