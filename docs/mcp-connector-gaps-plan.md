# Custom MCP connector — closing the gaps

Follow-up to `docs/mcp-connector-plan.md`, which shipped as commit `b45cffe8`.
Everything below is grounded in live probes against real MCP servers, recorded
inline so a reviewer can re-run them.

## Why this exists

The shipped connector supports `bearer`, `header`, and `none`. The customer's
single named example — Cloudflare's published MCP — requires OAuth, so the
headline use case does not connect. Verified against all six supported
combinations:

```
auth types supported: ('bearer', 'header', 'none')
  none    via streamable_http  -> REJECTED (401/403)
  none    via sse              -> REJECTED (401/403)
  bearer  via streamable_http  -> REJECTED (401/403)
  bearer  via sse              -> REJECTED (401/403)
  header  via streamable_http  -> REJECTED (401/403)
  header  via sse              -> REJECTED (401/403)
```

The server states the requirement outright:

```
www-authenticate: Bearer realm="OAuth",
  resource_metadata=".../.well-known/oauth-protected-resource/mcp"
```

What *does* work today: servers the customer builds themselves with a bearer
token. That is the ticket's primary mode, so this is a gap in coverage, not a
broken feature.

---

## PR 1 — Correctness fixes (~25 lines)

Three defects in merged behaviour. Independent of OAuth, no new surface, ship
first.

### 1a. `tools/list` pagination is ignored

`ClientSession.list_tools` does **not** auto-paginate — verified by reading the
SDK source. It takes a `cursor` and returns `nextCursor`, and we call it once:

```python
# connectors/mcp_connector/client.py, probe()
async def _list(session: ClientSession) -> List[Dict[str, Any]]:
    result = await session.list_tools()          # one page only
    return [t for t in (_clean_tool(t) for t in (result.tools or [])) if t]
```

A server with more tools than its page size silently loses the remainder.

**Honest caveat:** no server I probed actually paginates today. GitHub returns
all 49 tools with `nextCursor=None`. So this is latent, not active — but it is
10 lines and the failure mode is invisible.

```python
async def _list(session: ClientSession) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(MAX_TOOL_PAGES):          # bound it; a server could loop forever
        page = await session.list_tools(cursor)
        out.extend(t for t in (_clean_tool(t) for t in (page.tools or [])) if t)
        cursor = getattr(page, "nextCursor", None)
        if not cursor:
            break
    return out
```

The page bound is not optional. An adversarial or buggy server returning a
constant `nextCursor` would otherwise spin until the request times out.

### 1b. `MAX_TOOLS_PER_SERVER = 25` is below real-world servers

GitHub's MCP exposes **49 tools** (verified with a PAT). The cap truncates
roughly half.

This is *not* silent — registration already returns a warning:

```python
"warning": f"Only the first {MAX_TOOLS_PER_SERVER} tools were registered."
```

And with read-only enabled GitHub yields exactly 25 reads, so it fits by
coincidence. Still worth raising.

Raise to **64**, and keep the cap. The reason for a cap is prompt budget, not
politeness: every tool's name, description, and JSON schema enters the system
prompt on every turn. 64 tools across a 10-server limit is already a large
prompt contribution, and an unbounded cap lets one server crowd out Aurora's
own tools.

### 1c. `_is_auth_failure` does substring matching

```python
text = str(err)
if "401" in text or "403" in text or "Unauthorized" in text:
    return True
```

This misclassifies any error whose text happens to contain `401`/`403` — a
proxy rejection, a gateway page, a tool result mentioning a status code. I hit
this myself during testing and drew a wrong conclusion from it.

A real 401 arrives as `httpx.HTTPStatusError` nested in an `ExceptionGroup`:

```
ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  HTTPStatusError: Client error '401 Unauthorized' for url '...'
```

Match on the actual status code, walking the group:

```python
def _is_auth_failure(exc: BaseException) -> bool:
    """True when the exception chain carries a genuine HTTP 401/403.

    Status code only -- never the message text. A proxy's 403 body or a tool
    result mentioning "401" must not be reported to the user as "check your
    token", which sends them to fix a credential that was never the problem.
    """
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (401, 403):
        return True
    for sub in getattr(exc, "exceptions", ()) or ():
        if _is_auth_failure(sub):
            return True
    cause = exc.__cause__
    return cause is not None and _is_auth_failure(cause)
```

**Tests:** one per fix. Pagination needs a fake session returning two pages
then `nextCursor=None`, plus one that never stops (asserting the bound holds).
Auth detection needs a proxy-style error whose *text* contains `403` but whose
status is 502, asserting it is **not** classified as auth.

---

## PR 2 — OAuth 2.1 with Dynamic Client Registration (~250 lines)

### Why it is tractable

Every piece is verified working against Cloudflare:

| Step | Evidence |
|---|---|
| Resource metadata | `authorization_servers: ["https://observability.mcp.cloudflare.com"]` |
| AS metadata | `registration_endpoint`, `authorization_endpoint`, PKCE `S256` |
| DCR | POSTed a registration, got `client_id` back, `client_secret` absent (public client) |
| Grants | `authorization_code`, `refresh_token` |

DCR returning a public client with `token_endpoint_auth_method: "none"` is the
good case: no client secret to store, PKCE carries the security.

### Why the SDK's helper cannot be used directly

`mcp.client.auth.OAuthClientProvider` exists, but its signature is built for
desktop apps:

```python
OAuthClientProvider(server_url, client_metadata, storage,
                    redirect_handler=...,    # open a browser
                    callback_handler=...,    # BLOCK awaiting (code, state)
                    timeout=300.0)
```

`callback_handler` blocks awaiting the authorization code. Cursor and Claude
Desktop satisfy that by binding `localhost:PORT` in the same process. Aurora
cannot: the redirect lands as a **separate HTTP request**, possibly on a
different gunicorn worker, so there is no in-process handler to wake.

So: use the SDK's `OAuthClientMetadata` / `OAuthToken` / `PKCEParameters`
types, drive the flow with Aurora's own two-request pattern.

### Reuse, don't rebuild

Aurora already has this exact flow for Notion, Atlassian, OVH, PagerDuty,
SharePoint, Confluence:

- `utils/auth/oauth2_state_cache.py` — `store_oauth2_state(state, user_id,
  endpoint, project_id, code_verifier)`. **It already accepts
  `code_verifier`**, so PKCE needs no new storage.
- `routes/notion/notion_routes.py:51` — `_handle_oauth_callback` validates
  state, checks `user_id` match and `endpoint` match, rejects expired state.
  Copy this shape exactly; it is the security-critical part.
- Frontend: a callback page extracts `code`/`state` and `postMessage`s to the
  opener, which POSTs to the backend (`app/notion/callback/page.tsx:58`).

### What is genuinely new

Every existing Aurora OAuth connector talks to **one** known provider with a
pre-registered client ID in env (`NOTION_REDIRECT_URI`). Here the authorization
server is **discovered at runtime** and differs per customer server. DCR is
what makes that work without per-vendor onboarding.

### Shape

New module `connectors/mcp_connector/oauth.py` (~110 lines), pure functions,
no Flask:

```python
async def discover(server_url: str) -> AuthServerInfo:
    """Resolve the authorization server for an MCP endpoint.

    Follows the spec path: the 401's WWW-Authenticate names a
    resource_metadata URL, which names authorization_servers, whose metadata
    gives the endpoints. Falls back to the AS well-known on the server's own
    origin for servers that skip the resource-metadata hop.
    """

async def register_client(info: AuthServerInfo, redirect_uri: str) -> ClientCreds:
    """Dynamic Client Registration (RFC 7591). Raises if unsupported."""

def authorize_url(info, creds, redirect_uri, state, verifier) -> str:
    """Build the authorization URL with PKCE S256 challenge."""

async def exchange_code(info, creds, code, verifier, redirect_uri) -> OAuthToken:
async def refresh(info, creds, refresh_token) -> OAuthToken:
```

Two routes in `routes/mcp/mcp_routes.py` (~70 lines), both RBAC-decorated:

| Route | Does |
|---|---|
| `POST /mcp/servers/oauth/start` | discover, DCR, store state+verifier+client_id, return `authorizeUrl` |
| `POST /mcp/servers/oauth/complete` | validate state, exchange code, probe, store server |

The second must probe before storing — same invariant as today: a server is
only persisted after a successful handshake.

Auth blob gains:

```python
{"type": "oauth", "access_token": ..., "refresh_token": ...,
 "expires_at": ..., "client_id": ..., "token_endpoint": ...}
```

`build_headers` gets one branch returning the bearer access token.

### Refresh — the part that needs care

Access tokens expire, so every tool call can fail mid-investigation. Refresh
on demand inside `_run`:

```
call -> 401 -> refresh -> retry once -> persist new token
```

Two constraints that are easy to get wrong:

1. **Persist the rotated refresh token.** Most servers rotate it on use; losing
   the new one silently disconnects the server at the next expiry.
2. **Retry exactly once.** A refresh that yields a still-rejected token means
   the grant was revoked. Looping turns that into a hammer on the vendor's
   token endpoint.

This is where the `_is_auth_failure` fix in PR 1 becomes load-bearing: refresh
must trigger on a real 401, never on a proxy error.

Concurrency: two parallel tool calls can both see 401 and both refresh. With
one access token per (org, server) in a single Vault blob, the loser's write
clobbers the winner's.

`ponytail:` accept the race initially — worst case is one extra refresh and a
retry, both idempotent-ish. Note the ceiling: under heavy parallel tool use a
rotated-refresh-token server could invalidate the stored token. Upgrade path is
a short Redis lock keyed on `(org_id, label)` around the refresh.

### Frontend (~60 lines)

- Add `oauth` to the auth dropdown at `app/mcp/auth/page.tsx:196`.
- Selecting it hides the token field and changes Connect to: POST `/oauth/start`,
  open the returned URL in a popup, await `postMessage`, POST `/oauth/complete`.
- New `app/mcp/callback/page.tsx` (~35 lines), copied from the Notion one.
- Show OAuth servers with an "OAuth" badge and a Reconnect action for when a
  grant is revoked.

### The case that stays manual

Servers without a `registration_endpoint` cannot self-register. Those need the
user to create an OAuth app and paste client ID + secret. Add two optional
fields, surfaced only when discovery reports DCR unsupported. Not a blocker —
Cloudflare, Linear, Notion, and Sentry all support DCR.

### Security notes for review

- `discover()` and `register_client()` make outbound HTTP to a customer-supplied
  URL. **Both must go through `assert_allowed_target`**, same SSRF guard as the
  MCP connection itself. A URL that passes the MCP check but whose
  `authorization_servers` points at `169.254.169.254` is the obvious bypass.
- `authorization_servers` must be validated, not trusted. Pin it to the same
  origin as the MCP server unless we have a reason to allow cross-origin.
- `redirect_uri` must be an exact, fixed Aurora URL — never reflected from the
  request.
- Never log tokens, codes, verifiers, or the DCR response.

---

## Deliberately excluded

Written down as reasoned decisions, so this does not get re-litigated quarterly.

**stdio transport.** Running a customer-supplied binary inside Aurora's
container is remote code execution on a multi-tenant host whose environment
holds Vault credentials. Cursor can do it because it is your laptop running
your own config. The real fix is per-tenant sandboxed execution
(gVisor/Firecracker) — a project, not a feature. Remote HTTP only; say so in
the docs rather than leaving users to discover it.

**Sampling** (server requests an LLM completion from the client). A customer
server could drive Aurora's model spend and inject directly into the prompt
chain — and, worse, use it to **exfiltrate prior conversation context**, since
the sampling request shapes what gets sent to the model. Near-zero value for an
ops connector.

**Elicitation** (server asks the user a question mid-call). Needs a synchronous
human round-trip inside a tool invocation. Architecturally expensive, and
during RCA there is no human present.

**Resources** (`list_resources` / `read_resource`). Real surface — GitHub's MCP
exposes 4 resources and 2 prompts, verified. Deferred anyway: Aurora's agent
has no "server-provided context" slot, so fetching them has nothing to consume
it. Building that slot is a design question, not 60 lines of plumbing. Revisit
when a customer asks.

**Prompts** (`list_prompts` / `get_prompt`). Server-authored templates. Aurora
writes its own prompts and its RCA flow is opinionated; adopting customer
templates mid-investigation is a behaviour change, not a feature gap.

---

## Sequencing

All of this lands as **one PR on top of `b45cffe8`**, on the existing branch.
Internal ordering still matters during implementation:

| Order | Scope | Lines | Why first |
|---|---|---|---|
| 1 | pagination, cap, auth detection | ~25 | refresh-on-401 depends on correct auth detection |
| 2 | OAuth 2.1 + DCR + refresh | ~250 | builds on 1c |

Do 1c (`_is_auth_failure`) before the refresh logic: refresh must fire on a real
401 and never on a proxy error, so the detection has to be right first.

## Verification plan

PR 1 is unit-testable with fakes. PR 2 is not fully — the OAuth flow needs a
browser and a real authorization server.

- Extend `tests/chat/test_custom_mcp.py` for discovery parsing, PKCE challenge
  construction, state validation rejection paths, and expiry arithmetic. All
  pure functions, no network.
- Extend `tests/manual/mcp_e2e_check.py` with a DCR probe against Cloudflare —
  it already works unauthenticated and proves discovery plus registration
  end to end without a browser.
- The browser leg (authorize → consent → callback) stays a manual checklist.
  Target: connect `observability.mcp.cloudflare.com`, confirm tools appear,
  force-expire the access token, confirm one refresh recovers it.

## Open question for the customer

Network reachability is unresolved and OAuth does not touch it. From the Oct 2
call: *"The only thing is our network topologies, right? So how will that work
and through what will you go?"* If their self-built MCP servers sit inside their
network, Aurora SaaS cannot reach them regardless of auth — that is the Private
Link dependency, tracked separately. Worth separating these two threads
explicitly so OAuth is not mistaken for a connectivity answer.
