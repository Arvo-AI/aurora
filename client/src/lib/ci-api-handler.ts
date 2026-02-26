import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";
import type { CIProviderSlug } from "@/lib/services/ci-provider";

const API_BASE_URL = process.env.BACKEND_URL;

interface RouteConfig {
  slug: CIProviderSlug;
  endpoint: string;
  label: string;
}

export function createCIGetHandler({ slug, endpoint, label }: RouteConfig) {
  return async function GET(request: Request) {
    try {
      const authResult = await getAuthenticatedUser();
      if (authResult instanceof NextResponse) return authResult;

      const { headers: authHeaders } = authResult;
      const { searchParams } = new URL(request.url);
      const qs = searchParams.toString();

      const response = await fetch(
        `${API_BASE_URL}/${slug}/${endpoint}${qs ? `?${qs}` : ""}`,
        { method: "GET", headers: authHeaders, credentials: "include", cache: "no-store" }
      );

      if (!response.ok) {
        const text = await response.text();
        return NextResponse.json(
          { error: text || `Failed to fetch ${label}` },
          { status: response.status }
        );
      }

      const data = await response.json();
      return NextResponse.json(data);
    } catch (error) {
      console.error(`[api/${slug}/${endpoint}] Error:`, error);
      return NextResponse.json({ error: `Failed to load ${label}` }, { status: 500 });
    }
  };
}

export function createCIPostHandler({ slug, endpoint, label }: RouteConfig) {
  return async function POST(request: NextRequest) {
    try {
      const authResult = await getAuthenticatedUser();
      if (authResult instanceof NextResponse) return authResult;

      const { headers: authHeaders } = authResult;
      const payload = await request.json();

      const response = await fetch(`${API_BASE_URL}/${slug}/${endpoint}`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      });

      if (!response.ok) {
        const text = await response.text();
        return NextResponse.json(
          { error: text || `Failed to ${label}` },
          { status: response.status }
        );
      }

      const data = await response.json();
      return NextResponse.json(data);
    } catch (error) {
      console.error(`[api/${slug}/${endpoint}] Error:`, error);
      return NextResponse.json({ error: `Failed to ${label}` }, { status: 500 });
    }
  };
}
