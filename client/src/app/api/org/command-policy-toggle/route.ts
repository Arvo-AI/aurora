import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";

const API_BASE_URL = process.env.BACKEND_URL;

export async function PUT(req: NextRequest) {
  if (!API_BASE_URL) return NextResponse.json({ error: "BACKEND_URL not configured" }, { status: 500 });
  const authResult = await getAuthenticatedUser();
  if (authResult instanceof NextResponse) return authResult;
  const body = await req.json();
  const res = await fetch(`${API_BASE_URL}/api/org/command-policy-toggle`, {
    method: "PUT",
    headers: { ...authResult.headers, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return NextResponse.json(data, { status: res.status });
}
