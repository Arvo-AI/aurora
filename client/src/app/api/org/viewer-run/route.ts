import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

export async function GET(request: NextRequest) {
  return forwardRequest(request, "GET", "/api/org/viewer-run", "viewer run");
}

export async function PUT(request: NextRequest) {
  return forwardRequest(request, "PUT", "/api/org/viewer-run", "viewer run");
}
