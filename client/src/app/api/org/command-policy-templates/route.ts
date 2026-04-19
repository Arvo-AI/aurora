import { NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  if (!API_BASE_URL) return NextResponse.json({ error: "BACKEND_URL not configured" }, { status: 500 });
  const authResult = await getAuthenticatedUser();
  if (authResult instanceof NextResponse) return authResult;
  const res = await fetch(`${API_BASE_URL}/api/org/command-policy-templates`, {
    method: "GET",
    headers: { ...authResult.headers, "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  return NextResponse.json(data, { status: res.status });
}
