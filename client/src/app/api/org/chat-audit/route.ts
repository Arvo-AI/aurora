import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

export async function GET(request: NextRequest) {
  return forwardRequest(request, "GET", "/api/org/chat-audit", "chat audit");
}

export async function PUT(request: NextRequest) {
  return forwardRequest(request, "PUT", "/api/org/chat-audit", "chat audit");
}
