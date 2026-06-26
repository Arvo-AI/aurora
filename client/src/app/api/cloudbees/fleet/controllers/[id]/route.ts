import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth-helper";

const API_BASE_URL = process.env.BACKEND_URL;

// ---------------------------------------------------------------------------
// DELETE /api/cloudbees/fleet/controllers/[id]
// Removes a single controller from the user's standalone fleet.
// ---------------------------------------------------------------------------
export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { headers: authHeaders } = authResult;
    const { id } = await context.params;

    if (!API_BASE_URL) {
      return NextResponse.json({ error: "BACKEND_URL is not configured" }, { status: 500 });
    }

    const response = await fetch(
      `${API_BASE_URL.replace(/\/$/, "")}/cloudbees/fleet/controllers/${encodeURIComponent(id)}`,
      { method: "DELETE", headers: authHeaders, cache: "no-store" }
    );

    if (!response.ok) {
      await response.text();
      console.error(`[api/cloudbees/fleet/controllers] Backend error: ${response.status}`);
      return NextResponse.json(
        { error: "Failed to remove controller" },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("[api/cloudbees/fleet/controllers] Error:", error);
    return NextResponse.json({ error: "Failed to remove controller" }, { status: 500 });
  }
}
