import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ id: string }> }
) {
  const { id } = await context.params;
  return forwardRequest(
    request,
    "DELETE",
    `/cloudbees/fleet/controllers/${encodeURIComponent(id)}`,
    "remove fleet controller",
  );
}
