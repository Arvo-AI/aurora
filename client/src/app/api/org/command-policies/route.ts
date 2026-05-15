import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";

const API_BASE_URL = process.env.BACKEND_URL;
const TIMEOUT_MS = 20000;

async function proxyRequest(req: NextRequest, method: string, path: string, body?: unknown) {
  if (!API_BASE_URL) {
    return NextResponse.json({ error: "BACKEND_URL not configured" }, { status: 500 });
  }
  const authResult = await getAuthenticatedUser();
  if (authResult instanceof NextResponse) return authResult;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: { ...authResult.headers, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") {
      return NextResponse.json({ error: "Request timeout" }, { status: 504 });
    }
    return NextResponse.json({ error: "Backend request failed" }, { status: 500 });
  } finally {
    clearTimeout(timer);
  }
}

export async function GET() {
  return proxyRequest(new NextRequest("http://localhost"), "GET", "/api/org/command-policies");
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxyRequest(req, "POST", "/api/org/command-policies", body);
}
