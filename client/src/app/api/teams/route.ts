import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, "/teams", "teams");
}

async function deleteHandler(request: NextRequest) {
  return forwardRequest(request, "DELETE", "/teams", "teams", { passBody: false });
}

export { handler as GET, handler as POST, deleteHandler as DELETE };
