import { NextRequest } from 'next/server'
import { forwardAuthenticatedGet } from '@/lib/backend-proxy'

// GET /api/llm-models — user-selectable model catalog (with display metadata),
// honoring the backend's ENABLED_MODELS allowlist and LLM_PROVIDER_MODE routing.
// Proxies to the viewer-accessible /api/llm-config/models backend endpoint via
// the shared helper (auth, timeouts, error normalization handled there).
export async function GET(request: NextRequest) {
  return forwardAuthenticatedGet(request, '/api/llm-config/models', 'fetch model catalog')
}
