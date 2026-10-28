import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path = [] } = await params;
  const suffix = path.length ? `/${path.map(encodeURIComponent).join("/")}` : "";
  return forwardRequest(
    request,
    request.method,
    `/api/org/teams${suffix}`,
    "org teams",
  );
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
