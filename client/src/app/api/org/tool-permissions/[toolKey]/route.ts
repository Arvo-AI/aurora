import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ toolKey: string }> },
) {
  const { toolKey } = await params;
  return forwardRequest(request, "PUT", `/api/org/tool-permissions/${toolKey}`, "tool-permissions-toggle");
}
