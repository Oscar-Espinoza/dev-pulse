# Add an MCP server to DevPulse

## Context

**What an MCP server actually is, since this was the open question:** it is running code, not documentation. Today DevPulse publishes an OpenAPI spec at `/openapi.json`, which is documentation — a human reads it and writes a client. An MCP server is the layer above that: a live endpoint that hands an LLM a list of callable tools as JSON Schema at runtime, so the LLM invokes them itself.

Concretely, once this ships you can open chatgpt.com, go to Settings → Connectors → Developer Mode, paste `https://<your-render-host>/mcp`, and then ask *"which of my monitors went down last night, and what was the p50 latency on the checkout one?"* — ChatGPT will call `list_monitors` and `get_monitor_stats` against your real Render database. The same URL works in Claude.ai custom connectors, Claude Code, Cursor, and VS Code Copilot. A third party integrating with DevPulse does the same thing and writes no code.

**Why this is a real project rather than a wrapper:** the blocker is authentication. DevPulse authenticates with `X-API-Key` (`app/dependencies.py:20`) and deliberately has no passwords and no login — a decision `docs/study-guide/10-auth-and-users.md:10-13` records and defends twice. Claude Code and Cursor accept arbitrary headers, so a bearer token works there immediately. But OpenAI's connector documentation describes OAuth only (CIMD, dynamic client registration, PKCE) and does not document static API keys or custom headers. Supporting ChatGPT therefore means DevPulse must become an OAuth 2.1 authorization server.

The design that avoids reversing the no-passwords decision: **the API key stays the only credential.** The OAuth layer is a transport for it, not a new identity system. The consent screen asks the user to paste the DevPulse API key they already got from `POST /users`; the server validates it against `users.api_key` and mints an authorization code bound to that `user_id`. No passwords are stored, no external IdP is introduced, and multi-tenancy comes free because the data model is already per-user.

### Versions (verified 2026-08-17, all newer than the assistant's training data)

| Component | Version | Note |
|---|---|---|
| MCP specification | `2026-07-28` | Stateless core — no initialize handshake, no session IDs |
| `fastmcp` | `3.4.7` (2026-08-10) | Latest **stable**; implements spec ≤`2025-11-25` |
| `fastmcp` v4 | `4.0.0b3` (2026-08-14) | Beta; supports `2026-07-28`. Not chosen — beta on a portfolio project |
| official `mcp` SDK | `2.0.0` (2026-07-28) | Supports current spec but lower-level; renamed `FastMCP`→`MCPServer` |

FastMCP 3.4.7 is the choice. Current clients still negotiate `2025-11-25` happily — the new spec carries a minimum twelve-month deprecation window — and v3 is the only stable option with the FastAPI mounting, middleware, and `OAuthProvider` scaffolding this design leans on.

### Decisions taken

- Mount at `/mcp` inside the existing FastAPI app. One Render service, one process, one deploy. Tools reuse `app.state.session_factory` and `app.state.scheduler`, so they hit the same code paths as the routers — including the quota and minimum-interval checks at `app/routers/monitors.py:30-38`.
- Full CRUD (seven tools), plus an SSRF guard on the MCP path.
- Accept **both** OAuth access tokens and a raw API key as `Authorization: Bearer`. Costs about five lines in `verify_token` and means Claude Code works before any OAuth exists.

---

## Phase 1 — MCP server and tools

Ships something usable in Claude Code/Cursor on day one, before OAuth exists.

### `pyproject.toml`

Add `"fastmcp>=3.4.7,<4"` to `dependencies`. **Critically**, `[tool.setuptools] packages` at line 31 is an explicit list — add `"app.mcp"` or `pip install .` on Render silently omits the package.

### `app/services/url_guard.py` (new)

`async def assert_public_url(url: str) -> None`. Resolve the hostname with `socket.getaddrinfo`, then reject any resolved address where `ipaddress.ip_address(...)` reports `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, or `is_multicast`. Raises a `ValueError` the tool layer turns into a `ToolError`.

This closes, for the MCP surface only, the SSRF documented in `docs/study-guide/15-what-was-not-built.md` ("War story C5"). Document honestly in the docstring that it is a mitigation, not a cure — it does not defeat DNS rebinding, since `app/services/checker.py:45` re-resolves at check time. Leave the REST API's behavior untouched; changing it is a separate decision.

### `app/mcp/server.py` (new)

```python
def build_mcp_server(
    *,
    session_factory_getter: Callable[[], async_sessionmaker[AsyncSession]],
    scheduler_getter: Callable[[], MonitorScheduler],
    auth: AuthProvider | None,
) -> FastMCP:
```

Getters rather than values, because `session_factory` is created in the lifespan (`app/main.py:33`) and does not exist when `create_app()` runs. `create_app()` passes `lambda: app.state.session_factory`. This mirrors the existing `get_scheduler` indirection at `app/dependencies.py:40` and keeps tests able to inject their own without module-level globals or relying on Starlette scope-state propagation through the mount.

Seven tools, each a thin wrapper that resolves the caller, opens a session, and reuses the existing Pydantic schemas from `app/schemas.py` for both input and output so JSON Schema generation is automatic:

| Tool | Backing endpoint | Scope |
|---|---|---|
| `list_monitors` | `GET /monitors` | `devpulse:read` |
| `get_monitor` | `GET /monitors/{id}` | `devpulse:read` |
| `get_monitor_results` | `GET /monitors/{id}/results` | `devpulse:read` |
| `get_monitor_stats` | `GET /monitors/{id}/stats` | `devpulse:read` |
| `create_monitor` | `POST /monitors` | `devpulse:write` |
| `update_monitor` | `PATCH /monitors/{id}` | `devpulse:write` |
| `delete_monitor` | `DELETE /monitors/{id}` | `devpulse:write` |

`create_monitor` and `update_monitor` call `assert_public_url` before persisting, then `scheduler.sync_monitor(...)` exactly as `app/routers/monitors.py` does.

Write clear, task-oriented docstrings — they become the tool descriptions the model reads, and they are the single biggest lever on whether the LLM picks the right tool. `get_monitor_stats` should say it answers "is this up, and how fast" over a window, so the model reaches for it instead of pulling 500 raw results.

Reuse `_get_owned_monitor` (`app/routers/monitors.py:55-64`) — extract it to a shared helper rather than duplicating it, so the 404-not-403 ownership behavior stays in one place.

### `app/mcp/auth.py` (new) — Phase 1 portion

`DevPulseTokenVerifier(TokenVerifier)` with `verify_token(token)` looking the string up against `users.api_key` and returning an `AccessToken` carrying `user_id` and both scopes. Phase 2 subsumes this into the OAuth provider.

### `app/main.py`

```python
mcp = build_mcp_server(
    session_factory_getter=lambda: app.state.session_factory,
    scheduler_getter=lambda: app.state.scheduler,
    auth=auth,
)
mcp_app = mcp.http_app(path="/")
app.mount("/mcp", mcp_app)
```

The existing `lifespan` must be merged with `mcp_app.lifespan` via `fastmcp.utilities.lifespan.combine_lifespans` — the MCP app's lifespan sets up its transport, and skipping it produces a `RuntimeError` on first request. Gate the whole mount behind a `mcp_enabled` setting so the app still boots if MCP is misconfigured.

### Tests

`tests/conftest.py:69` builds a bare `FastAPI()` and includes routers directly rather than calling `create_app()`, so **the existing fixtures will not exercise the mount**. Add a separate `mcp_app` fixture that calls `build_mcp_server` with the test `session_factory` and `scheduler_spy`, and drive it with `fastmcp.Client` in-memory — no HTTP, consistent with the existing hermetic sqlite approach. Cover: each tool's happy path, cross-user isolation returning not-found, the quota and interval errors surfacing as `ToolError`, and `assert_public_url` rejecting `127.0.0.1`, `169.254.169.254`, and `10.0.0.1`.

---

## Phase 2 — OAuth 2.1 authorization server

This is what unlocks chatgpt.com. FastMCP's `OAuthProvider` base class supplies the `/authorize`, `/token`, `/register`, and `/.well-known/*` route scaffolding; `InMemoryOAuthProvider` in `fastmcp_slim/fastmcp/server/auth/providers/in_memory.py` is the reference implementation to follow — it shows exactly which four stores and which methods a subclass must provide.

### `app/models.py` + one Alembic migration

Three tables, following the existing `Mapped[]` declarative style and `JSONVariant` (`app/models.py:28`):

- `oauth_clients` — `client_id` PK, `client_secret`, `redirect_uris` (JSON), `grant_types`, `scope`, `client_name`, `created_at`. Populated by dynamic client registration.
- `oauth_authorization_codes` — `code` PK, `client_id`, `user_id` FK→`users.id` ON DELETE CASCADE, `redirect_uri`, `code_challenge` (PKCE S256), `scopes`, `expires_at`, `used`. Must be single-use; that is why this is a table and not a signed token.
- `oauth_tokens` — `token` PK, `kind` (`access`/`refresh`), `client_id`, `user_id` FK, `scopes`, `expires_at`, `revoked`. Stored rather than JWT so revocation is possible and the Render free tier's sleep cycle cannot invalidate anything.

Generate the migration with `alembic revision --autogenerate`; `alembic/env.py:40` already reuses the app's `build_engine`, so no configuration changes are needed.

### `app/mcp/auth.py` — `DevPulseOAuthProvider(OAuthProvider)`

Implements `get_client`, `register_client`, `authorize`, `load_authorization_code`, `exchange_authorization_code`, `load_access_token`, `load_refresh_token`, `exchange_refresh_token`, and `revoke_token` against those tables.

`authorize(client, params)` returns the URL of the local consent page, carrying an opaque transaction id.

Override `verify_token` to try the `oauth_tokens` table first and fall back to the `users.api_key` lookup, so both auth styles work against one server.

### `app/routers/oauth.py` (new) — consent screen

- `GET /oauth/consent?txn=...` returns an `HTMLResponse` (inline string — do not add Jinja2) showing which client is asking, the scopes requested, and a single password-type field for the DevPulse API key.
- `POST /oauth/consent` validates the key against `users.api_key`, mints the authorization code bound to that `user_id`, and 302s to the client's `redirect_uri` with `code` and `state`.

Optionally offer "don't have a key? enter your email" wired to the existing `POST /users` — genuinely better UX, but scope creep; defer unless the flow feels broken in testing.

### `app/config.py`

Add `mcp_enabled: bool = True` and `mcp_base_url: str | None = None`. `extra="ignore"` (line 10) means new env vars are safe.

### Mounting the discovery routes — the highest-risk step

FastMCP's invariant is **`base_url + mcp_path` must equal the externally reachable MCP URL**. With the app mounted at `/mcp`, set `base_url` to the bare public origin (`https://<host>`) and `mcp_path` to `/mcp`.

Discovery metadata must be served from the **root**, not from under the mount. Add `auth.get_well_known_routes(mcp_path="/mcp")` to the parent FastAPI router so `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp` resolve at the top level. If these land under `/mcp/.well-known/...`, every client fails discovery with an opaque error — this is the single most common way this integration breaks, so verify with `curl` before touching a real client.

---

## Phase 3 — Deployment and docs

- **`render.yaml`**: add `MCP_BASE_URL` as the public service URL. Keep `startCommand` single-process with no `--workers` — the in-process scheduler still requires it (`docs/study-guide/00-orientation.md`). Migrations already run inline on deploy.
- **`README.md`**: a section with the connector URL and per-client setup — ChatGPT Developer Mode, Claude.ai custom connector, and `claude mcp add --transport http devpulse https://<host>/mcp --header "Authorization: Bearer <api-key>"`.
- **`docs/study-guide/16-mcp-server.md`**: the repo is decision-focused and plan-first. The interesting decisions to record are why the API key became the OAuth credential rather than adding passwords, why the tools call the service layer instead of the app's own HTTP API, and why the SSRF guard is applied only on the MCP path.
- Note in the README that Render's free tier sleeps; the first connector request after idle may exceed a client's timeout and need a retry.

---

## Verification

1. `pytest` — all existing 53 tests plus the new MCP tests.
2. `uvicorn app.main:app --reload`, then `npx @modelcontextprotocol/inspector` against `http://localhost:8000/mcp` — confirms the tool list, schemas, and a live `list_monitors` call.
3. `curl -s localhost:8000/.well-known/oauth-authorization-server | jq` and `.../oauth-protected-resource/mcp` — confirm every advertised URL is absolute and actually resolves. Check `/mcp` without a token returns `401` with a `WWW-Authenticate` header pointing at the metadata.
4. Bearer path: `claude mcp add --transport http devpulse http://localhost:8000/mcp --header "Authorization: Bearer <api-key>"`, then ask Claude Code to list monitors.
5. Full OAuth loop locally with `fastmcp.Client` using its OAuth helper — exercises registration, consent, code exchange, and an authenticated tool call.
6. Deploy, then add the connector in ChatGPT Developer Mode and in Claude.ai. Confirm the consent screen renders, the redirect returns cleanly, and a tool call succeeds.
7. Adversarial check: ask the connected LLM to create a monitor pointing at `http://169.254.169.254/latest/meta-data/` and confirm it is refused.

## Risks

- **ChatGPT connector support is the least documented part of this.** OpenAI's docs describe CIMD, DCR, and PKCE; FastMCP 3's `OAuthProvider` implements DCR. If ChatGPT insists on CIMD, a small `client.json` handling path may need adding. Note also that spec `2026-07-28` deprecates DCR in favor of CIMD — this design is correct today and has a twelve-month runway, but it is the part most likely to need revisiting.
- **`POST /users` is unauthenticated**, so quota enforcement stays advisory. Unchanged by this work, but worth restating in the study-guide chapter now that a second front door exists.

---

## What changed during implementation

Kept for the record, since this repo treats plans as history rather than
documentation. Four things came out differently:

- **No shared helper extraction.** The plan proposed pulling `_get_owned_monitor`
  and the quota checks into a shared module. The MCP tools instead call the route
  handlers in `app/routers/monitors.py` directly — they are plain async functions
  and the `Depends` annotations only bind inside FastAPI's router. Strictly less
  code and the two surfaces cannot drift.
- **A fourth OAuth table.** `oauth_transactions` was not in the plan. It parks an
  authorization request while the user is on the consent page, so that page
  carries only an opaque id and a tampered `redirect_uri` cannot survive.
- **Epoch integers, not timestamps.** OAuth expiries are `BigInteger`. SQLite
  discards timezone information, so a `DateTime(timezone=True)` column returns a
  naive value that cannot be compared against an aware "now".
- **`build_mcp_components` / `mount_mcp`.** Extracted from `create_app` so the
  tests assemble the MCP surface through the same code the deployed app uses.

All four, and the discovery-path trap the plan flagged as highest risk, are
written up in `docs/study-guide/16-mcp-server.md`.
