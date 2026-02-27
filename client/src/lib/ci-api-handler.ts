import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";
import type { CIProviderSlug } from "@/lib/services/ci-provider";

const API_BASE_URL = process.env.BACKEND_URL;

function getApiBaseUrl(): string {
  if (!API_BASE_URL) {
    throw new Error("BACKEND_URL is not configured");
  }
  return API_BASE_URL.replace(/\/$/, "");
}

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs = 15000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

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

      const apiBaseUrl = getApiBaseUrl();
      const response = await fetchWithTimeout(
        `${apiBaseUrl}/${slug}/${endpoint}${qs ? `?${qs}` : ""}`,
        { method: "GET", headers: authHeaders, cache: "no-store" }
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

      let payload: unknown;
      try {
        payload = await request.json();
      } catch {
        return NextResponse.json({ error: "Invalid JSON payload" }, { status: 400 });
      }

      const apiBaseUrl = getApiBaseUrl();
      const response = await fetchWithTimeout(`${apiBaseUrl}/${slug}/${endpoint}`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
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
