import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

export async function POST(request: NextRequest) {
  return forwardRequest(request, "POST", "/api/org/tool-permissions/seed", "tool-permissions-seed");
}
