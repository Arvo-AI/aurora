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

    // Authenticated user
    const response = await fetch(`${API_BASE_URL}/api/azure-subscriptions?user_id=${userId}`, {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
        "X-User-ID": userId,
      },
    });

    if (!response.ok) {
      let errorData;
      try {
        const jsonData = await response.json();
        errorData = jsonData.error || jsonData.message || "Failed to fetch Azure subscriptions";
      } catch {
        errorData = await response.text() || "Failed to fetch Azure subscriptions";
      }
      console.error("Backend azure-subscriptions error:", errorData);
      return NextResponse.json(
        { error: errorData },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: any) {
    console.error("Error in Azure subscriptions API:", error);
    return NextResponse.json(
      { error: "Internal server error" },
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

    // Authenticated user
    const body = await request.json();

    const response = await fetch(`${API_BASE_URL}/api/azure-subscriptions?user_id=${userId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-ID": userId,
      },
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      let errorData;
      try {
        const jsonData = await response.json();
        errorData = jsonData.error || jsonData.message || "Failed to update Azure subscription preferences";
      } catch {
        errorData = await response.text() || "Failed to update Azure subscription preferences";
      }
      console.error("Backend azure-subscriptions POST error:", errorData);
      return NextResponse.json(
        { error: errorData },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: any) {
    console.error("Error in Azure subscriptions POST API:", error);
    return NextResponse.json(
      { error: "Internal server error" },
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