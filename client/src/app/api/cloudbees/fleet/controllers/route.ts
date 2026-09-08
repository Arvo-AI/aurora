import { NextRequest } from "next/server";
import { forwardRequest } from "@/lib/backend-proxy";

const BACKEND_PATH = "/cloudbees/fleet/controllers";

export async function GET(request: NextRequest) {
  return forwardRequest(request, "GET", BACKEND_PATH, "CloudBees controller fleet");
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, "POST", BACKEND_PATH, "add controllers to fleet");
}
