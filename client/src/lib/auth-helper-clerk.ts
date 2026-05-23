/**
 * Clerk-based auth helper for SaaS mode.
 * Drop-in replacement for auth-helper.ts when NEXT_PUBLIC_SAAS_MODE=true.
 */
import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

export interface AuthResult {
  userId: string;
  orgId?: string;
  headers: Record<string, string>;
}

const INTERNAL_API_SECRET = process.env.INTERNAL_API_SECRET || "";

export async function getAuthenticatedUser(): Promise<AuthResult | NextResponse> {
  const { userId, orgId } = await auth();

  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const headers: Record<string, string> = {
    "X-User-ID": userId,
  };

  if (orgId) {
    headers["X-Org-ID"] = orgId;
  }

  if (INTERNAL_API_SECRET) {
    headers["X-Internal-Secret"] = INTERNAL_API_SECRET;
  }

  return { userId, orgId: orgId || undefined, headers };
}

export async function makeAuthenticatedRequest(
  url: string,
  options: RequestInit = {},
  additionalHeaders: Record<string, string> = {}
): Promise<Response> {
  const authResult = await getAuthenticatedUser();

  if (authResult instanceof NextResponse) {
    throw new Error("User not authenticated");
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  if (options.signal) {
    options.signal.addEventListener("abort", () => controller.abort());
  }

  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        ...options.headers,
        ...authResult.headers,
        ...additionalHeaders,
      },
    });
  } finally {
    clearTimeout(timeout);
  }
}
