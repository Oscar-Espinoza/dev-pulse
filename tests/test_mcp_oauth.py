"""The OAuth 2.1 authorization code flow, end to end.

This is the path ChatGPT takes. It is also the only place a DevPulse API key
is exchanged for anything, so the interesting assertions are about what the
consent step will and will not accept.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

REDIRECT_URI = "http://localhost/callback"


def _pkce() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


async def _register(http) -> dict:
    response = await http.post(
        "/mcp/register",
        json={
            "client_name": "Test Chat Client",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _start_authorization(http, client: dict, challenge: str) -> str:
    """Hit /authorize and return the consent transaction id."""
    response = await http.get(
        "/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": "devpulse:read devpulse:write",
        },
    )
    assert response.status_code in (302, 303, 307), response.text
    location = response.headers["location"]
    assert "/oauth/consent" in location, location
    return parse_qs(urlparse(location).query)["txn"][0]


async def _approve(http, txn: str, api_key: str) -> str:
    """Submit the consent form and return the authorization code."""
    response = await http.post(
        "/oauth/consent", data={"txn": txn, "api_key": api_key}
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI), location
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["opaque-state"]
    return query["code"][0]


async def _redeem(http, client: dict, code: str, verifier: str):
    return await http.post(
        "/mcp/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "code_verifier": verifier,
        },
    )


# --- discovery ------------------------------------------------------------

async def test_discovery_metadata_is_served_from_the_root(mcp_http) -> None:
    """Clients look here first; under the /mcp mount they would never find it."""
    async with mcp_http() as http:
        resource = await http.get("/.well-known/oauth-protected-resource/mcp")
        server = await http.get("/.well-known/oauth-authorization-server/mcp")

    assert resource.status_code == 200
    assert resource.json()["resource"] == "http://localhost/mcp"

    assert server.status_code == 200
    metadata = server.json()
    assert metadata["issuer"] == "http://localhost/mcp"
    assert metadata["authorization_endpoint"] == "http://localhost/mcp/authorize"
    assert metadata["token_endpoint"] == "http://localhost/mcp/token"
    assert metadata["registration_endpoint"] == "http://localhost/mcp/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]


async def test_advertised_endpoints_actually_resolve(mcp_http) -> None:
    """Guards the classic failure: metadata pointing at paths that 404."""
    async with mcp_http() as http:
        metadata = (
            await http.get("/.well-known/oauth-authorization-server/mcp")
        ).json()
        for key in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
            probe = await http.post(metadata[key])
            assert probe.status_code != 404, f"{key} -> {metadata[key]} is not mounted"


async def test_unauthenticated_mcp_request_points_at_the_metadata(mcp_http) -> None:
    """The 401 has to name where the metadata lives, or discovery cannot start."""
    async with mcp_http() as http:
        response = await http.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers.get("www-authenticate", "")


# --- the full flow --------------------------------------------------------

async def test_authorization_code_flow_yields_a_working_token(
    mcp_http, mcp_client_factory, user
) -> None:
    verifier, challenge = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        code = await _approve(http, txn, user.api_key)

        tokens = (await _redeem(http, client, code, verifier)).json()
        assert tokens["token_type"].lower() == "bearer"
        assert tokens["refresh_token"]

        # The minted token must work as an MCP bearer credential.
        async with mcp_client_factory(tokens["access_token"]) as mcp:
            listed = await mcp.list_tools()
            created = await mcp.call_tool(
                "create_monitor",
                {"url": "https://example.com/health", "interval_seconds": 300},
            )

    assert len(listed) == 7
    assert created.data.user_id == user.id


async def test_consent_page_names_the_client_and_its_scopes(mcp_http) -> None:
    _, challenge = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        page = await http.get("/oauth/consent", params={"txn": txn})

    assert page.status_code == 200
    assert "Test Chat Client" in page.text
    assert "See your monitors" in page.text
    assert "Create, change and delete your monitors" in page.text


async def test_wrong_api_key_is_refused_and_offers_a_retry(mcp_http) -> None:
    _, challenge = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        response = await http.post(
            "/oauth/consent", data={"txn": txn, "api_key": "wrong-key"}
        )

    assert response.status_code == 401
    assert "not valid" in response.text
    assert 'name="api_key"' in response.text  # the form is still usable


async def test_authorization_code_is_single_use(
    mcp_http, mcp_client_factory, user
) -> None:
    """A replayed code must not mint a second token."""
    verifier, challenge = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        code = await _approve(http, txn, user.api_key)

        first = await _redeem(http, client, code, verifier)
        replay = await _redeem(http, client, code, verifier)

    assert first.status_code == 200
    # 401 rather than OAuth's usual 400: the MCP spec requires invalid or
    # expired tokens to answer 401, and FastMCP rewrites the status to match.
    assert replay.status_code == 401
    assert replay.json()["error"] == "invalid_grant"


async def test_pkce_verifier_must_match(mcp_http, user) -> None:
    _, challenge = _pkce()
    wrong_verifier, _ = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        code = await _approve(http, txn, user.api_key)
        response = await _redeem(http, client, code, wrong_verifier)

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_grant"


async def test_refresh_token_returns_a_fresh_access_token(mcp_http, user) -> None:
    verifier, challenge = _pkce()
    async with mcp_http() as http:
        client = await _register(http)
        txn = await _start_authorization(http, client, challenge)
        code = await _approve(http, txn, user.api_key)
        tokens = (await _redeem(http, client, code, verifier)).json()

        refreshed = await http.post(
            "/mcp/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
                "client_secret": client.get("client_secret", ""),
            },
        )

    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != tokens["access_token"]


async def test_expired_transaction_is_rejected(mcp_http, oauth_provider) -> None:
    with pytest.raises(Exception):
        await oauth_provider.describe_transaction("no-such-transaction")


# --- both credential styles reach the same tools --------------------------

async def test_raw_api_key_still_works_as_a_bearer_token(
    mcp_http, mcp_client_factory, user
) -> None:
    """Claude Code and Cursor send the API key directly, with no OAuth at all."""
    async with mcp_http():
        async with mcp_client_factory(user.api_key) as mcp:
            tools = await mcp.list_tools()
    assert len(tools) == 7
