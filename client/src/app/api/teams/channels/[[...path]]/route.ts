import { NextRequest, NextResponse } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

// Proxies /api/teams/channels/** to the Flask backend /teams/channels/**.
// Kept separate from /api/teams (connect/status/disconnect) so channel-management
// endpoints reach the backend through the required proxy boundary.
async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  if (path?.some((segment) => segment === "." || segment === ".." || segment.includes("/"))) {
    return NextResponse.json({ error: "Invalid Teams channel path" }, { status: 400 });
  }
  const suffix = path?.length ? "/" + path.map(encodeURIComponent).join("/") : "";
  const backendPath = "/teams/channels" + suffix;
  return forwardRequest(request, request.method, backendPath, "teams");
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };
