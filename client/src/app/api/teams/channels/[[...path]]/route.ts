import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  const suffix = path && path.length ? "/" + path.join("/") : "";
  const backendPath = "/teams/channels" + suffix;
  return forwardRequest(request, request.method, backendPath, "teams");
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };
