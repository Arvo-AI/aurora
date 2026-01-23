import { NextRequest, NextResponse } from "next/server"

import { makeAuthenticatedRequest } from "@/lib/auth-helper"

const BACKEND_URL = process.env.BACKEND_URL

export async function GET(request: NextRequest) {
  try {
    if (!BACKEND_URL) {
      return NextResponse.json({ error: "Backend URL is not configured" }, { status: 500 })
    }

    const sessionId = request.nextUrl.searchParams.get("session_id")

    if (!sessionId) {
      return NextResponse.json({ error: "Missing session_id" }, { status: 400 })
    }

    const url = new URL(`${BACKEND_URL}/terraform/workspace/files`)
    url.searchParams.set("session_id", sessionId)

    const response = await makeAuthenticatedRequest(url.toString(), { method: "GET" })
    const data = await response.json()

    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    return NextResponse.json({ error: "Failed to load workspace files" }, { status: 500 })
  }
}
