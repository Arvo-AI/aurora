import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";

const API_BASE_URL = process.env.BACKEND_URL;
const TIMEOUT_MS = 20000;

async function proxy(method: string, path: string, body?: unknown) {
  if (!API_BASE_URL) return NextResponse.json({ error: "BACKEND_URL not configured" }, { status: 500 });
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
    if (e instanceof Error && e.name === "AbortError") return NextResponse.json({ error: "Timeout" }, { status: 504 });
    return NextResponse.json({ error: "Backend request failed" }, { status: 500 });
  } finally {
    clearTimeout(timer);
  }
}

export async function PUT(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await req.json();
  return proxy("PUT", `/api/org/command-policies/${id}`, body);
}

export async function DELETE(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxy("DELETE", `/api/org/command-policies/${id}`);
}
