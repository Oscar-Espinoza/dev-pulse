# DevPulse

DevPulse is an uptime & latency monitoring API. Register a monitor — a URL,
HTTP method, expected status code, and check interval — and DevPulse checks
it on schedule, recording every result (success, timeout, connection error,
wrong status) so you can query its history.

It also speaks MCP, so you can connect it to ChatGPT, Claude, or Cursor and
ask *"which of my monitors went down last night?"* in plain English —
see [Use it from an LLM](#use-it-from-an-llm-mcp).

Live at **https://dev-pulse-wubf.onrender.com** ([docs](https://dev-pulse-wubf.onrender.com/docs)).

Portfolio project built to practice a production-shaped FastAPI backend:
typed async ORM models, a dynamically-scheduled background job system, and
per-user rate limiting, all wired through FastAPI's lifespan.

## Tech Stack

- **FastAPI** — API layer, dependency injection, lifespan-managed startup/shutdown
- **SQLAlchemy 2.0** (async, typed `Mapped[]` models) + **Postgres** (Neon-compatible)
- **APScheduler 3.x** (`AsyncIOScheduler`) — one recurring job per active monitor
- **httpx** — async HTTP checks
- **Alembic** — schema migrations
- **structlog** — structured logging
- **FastMCP 3** — MCP server mounted at `/mcp`, with a built-in OAuth 2.1 provider
- **pytest** + **pytest-asyncio** — async test suite (in-memory SQLite, mocked HTTP)

## How it works

```
FastAPI lifespan
 ├─ async engine (Neon-aware URL normalization, pooler-safe)
 ├─ shared httpx.AsyncClient
 └─ MonitorScheduler (APScheduler AsyncIOScheduler)
      ├─ on startup: schedules one interval job per active monitor
      ├─ on API create/update/delete: syncs exactly that monitor's job
      └─ each run: HTTP probe -> CheckResult row (never crashes the loop)
```

Monitor CRUD reschedules just the one affected job rather than reloading
everything. Per-user limits (configurable): max 20 active monitors, minimum
60s interval.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # point DATABASE_URL at your Postgres

alembic upgrade head
uvicorn app.main:app --reload
```

```bash
# register and grab your API key
curl -s -X POST localhost:8000/users -H 'content-type: application/json' \
  -d '{"email":"me@example.com"}'

# create a monitor checked every 60s
curl -s -X POST localhost:8000/monitors -H "X-API-Key: <your key>" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","interval_seconds":60}'

# uptime %, avg latency, and incident count over the last 24h
curl -s "localhost:8000/monitors/<monitor id>/stats?hours=24" -H "X-API-Key: <your key>"
```

Interactive docs: http://localhost:8000/docs

## Use it from an LLM (MCP)

DevPulse serves a [Model Context Protocol](https://modelcontextprotocol.io)
endpoint at `/mcp`. This is **running code, not documentation** — a chat client
connects to it, reads the tool list at request time, and calls your real API
against your real database. Ask *"which of my monitors went down last night?"*
and it answers from live data.

The connector URL is:

```
https://dev-pulse-wubf.onrender.com/mcp
```

### Step 1 — get an API key

The same key the REST API uses. If you don't have one yet:

```bash
curl -s -X POST https://dev-pulse-wubf.onrender.com/users \
  -H 'content-type: application/json' -d '{"email":"me@example.com"}'
```

Save the `api_key` from the response — there's no endpoint to retrieve it
again. (It is stored in plaintext, not hashed; see *Known limitations*.)

### Step 2 — connect your client

**Clients that accept a header** (Claude Code, Cursor, VS Code Copilot) — send
the key as a bearer token; no OAuth involved:

```bash
claude mcp add --transport http devpulse https://dev-pulse-wubf.onrender.com/mcp \
  --header "Authorization: Bearer <your api key>"
```

**Chat clients that require OAuth** (ChatGPT, Claude.ai) — paste the connector
URL and DevPulse walks you through its own consent page, where you paste the
same API key:

- **ChatGPT** — Settings → Connectors → Advanced → Developer mode, then add a
  custom connector pointing at the URL above.
- **Claude.ai** — Settings → Connectors → Add custom connector, same URL.

Nothing else to configure: the client discovers the OAuth endpoints, scopes and
tool list on its own.

### Step 3 — ask it things

```
"List my monitors and tell me which ones are failing."
"What's the uptime and average latency on the checkout monitor this week?"
"Add a monitor for https://example.com/health every 5 minutes."
"Show me the last 20 check results for monitor 3 — any pattern to the timeouts?"
```

### The seven tools

| Tool | Does | Scope |
|---|---|---|
| `list_monitors` | All your monitors | `devpulse:read` |
| `get_monitor` | One monitor's configuration | `devpulse:read` |
| `get_monitor_stats` | Uptime %, avg latency, incidents over a window | `devpulse:read` |
| `get_monitor_results` | Raw recent check results | `devpulse:read` |
| `create_monitor` | Register a new URL to watch | `devpulse:write` |
| `update_monitor` | Change URL, interval, expected status, active flag | `devpulse:write` |
| `delete_monitor` | Remove a monitor and its history | `devpulse:write` |

Tools enforce the same rules as the REST API — per-user quota, minimum
interval, and monitors you don't own reporting as not-found — because they call
the same route handlers. On the MCP path only, `create_monitor` and
`update_monitor` additionally refuse private, loopback and cloud-metadata
addresses: those URLs come from a language model, which can be steered by text
it read elsewhere.

### How the auth works, and why

DevPulse acts as its own OAuth 2.1 authorization server (dynamic client
registration, PKCE, refresh tokens). It deliberately has **no passwords**: the
consent screen asks for the API key you received from `POST /users`, so OAuth
is only a way to *carry* that credential to clients that cannot send a header.
Presenting the key is both proof of identity and the act of granting consent.

### Configuration

`MCP_BASE_URL` must be the service's public origin, HTTPS, no trailing slash
and no `/mcp` suffix — the code appends the path itself. Discovery metadata
advertises absolute URLs built from it, so a wrong value means clients fail to
connect with an unhelpful error. `MCP_ENABLED=false` removes the entire surface
(endpoint, discovery routes and consent page) and leaves the REST API running.

### Troubleshooting

Check what the server is actually advertising:

```bash
curl -s https://dev-pulse-wubf.onrender.com/.well-known/oauth-protected-resource/mcp
curl -s https://dev-pulse-wubf.onrender.com/.well-known/oauth-authorization-server/mcp
```

| Symptom | Cause |
|---|---|
| Client won't connect; no consent page appears | `MCP_BASE_URL` doesn't match the URL clients actually use |
| `404` on `/mcp` | `MCP_ENABLED=false`, or the deploy predates this feature |
| First request times out, retry works | Render free tier was asleep (see *Known limitations*) |
| Tool list comes back empty | Not authenticated — tools are hidden, not rejected, without a valid token |

## Tests

```bash
pytest
```

No network and no Postgres required — tests run on in-memory SQLite with
mocked HTTP.

## Known limitations

This is a portfolio project, not a hardened production service:

- **API keys are stored in plaintext**, not hashed — anyone who can read the
  `users` table can authenticate as any user. The most valuable fix in the
  codebase; see study guide chapter 15.
- Monitors created through the **REST API can target any URL**, including
  internal addresses. The MCP surface does block private and link-local
  targets, because there the URL comes from a language model.
- **Per-user quotas aren't enforced across accounts**, since registration has
  no verification step — `POST /users` is unauthenticated.
- Check results are **never pruned**; one monitor is roughly half a million
  rows a year.

On Render's free tier the service sleeps when idle, so the first MCP request
after a quiet period may time out in the client and need a retry.

## Learn how it works

`docs/study-guide/` is a 17-chapter walkthrough of this codebase written
decision-first — why each choice was made, what was rejected, and nine war
stories about things that actually broke. Start at
[chapter 00](docs/study-guide/00-orientation.md); the MCP server is
[chapter 16](docs/study-guide/16-mcp-server.md).
