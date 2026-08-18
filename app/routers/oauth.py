"""The consent screen: the one human-facing page DevPulse serves.

An OAuth authorization server has to put a real person in front of a real
decision. DevPulse has no login, so this page asks for the credential the user
already holds — their API key — and treats presenting it as both proof of
identity and the act of granting consent.

The HTML is an inline string rather than a template. One page does not justify
adding Jinja2 and a templates directory to a service that is otherwise pure
JSON, and keeping the markup next to the handler makes the whole flow readable
in one sitting.
"""

import html

import structlog
from fastapi import APIRouter, Form, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.mcp.oauth import ConsentError, DevPulseOAuthProvider

logger = structlog.stdlib.get_logger(__name__)

SCOPE_DESCRIPTIONS = {
    "devpulse:read": "See your monitors and their check history",
    "devpulse:write": "Create, change and delete your monitors",
}

_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {client} &middot; DevPulse</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #f4f5f7; color: #16181d; padding: 1.5rem;
  }}
  main {{
    background: #fff; max-width: 26rem; width: 100%;
    padding: 2rem; border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,.12), 0 8px 24px rgba(0,0,0,.06);
  }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .25rem; }}
  p.lede {{ margin: 0 0 1.25rem; color: #5b6270; }}
  ul {{ margin: 0 0 1.5rem; padding-left: 1.1rem; color: #353b45; }}
  li {{ margin-bottom: .35rem; }}
  label {{ display: block; font-weight: 600; margin-bottom: .4rem; }}
  input {{
    width: 100%; padding: .6rem .7rem; font-size: 1rem; font-family: inherit;
    border: 1px solid #c9cdd6; border-radius: 8px; box-sizing: border-box;
  }}
  button {{
    width: 100%; margin-top: 1rem; padding: .7rem; font-size: 1rem;
    font-weight: 600; color: #fff; background: #2b6cb0; cursor: pointer;
    border: 0; border-radius: 8px;
  }}
  button:hover {{ background: #245a94; }}
  .error {{
    background: #fdecec; border: 1px solid #f5aeae; color: #8a1c1c;
    padding: .6rem .75rem; border-radius: 8px; margin-bottom: 1rem;
  }}
  .hint {{ font-size: .85rem; color: #5b6270; margin-top: 1rem; }}
  code {{ background: #eef0f4; padding: .1rem .3rem; border-radius: 4px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e8eaee; }}
    main {{ background: #1e2127; box-shadow: none; }}
    p.lede, .hint {{ color: #a0a7b4; }}
    ul {{ color: #cbd1db; }}
    input {{ background: #14161a; color: #e8eaee; border-color: #39404c; }}
    code {{ background: #2a2f38; }}
  }}
</style>
</head>
<body>
<main>
  <h1>Authorize {client}</h1>
  <p class="lede">It is asking to connect to your DevPulse account and:</p>
  <ul>{scopes}</ul>
  {error}
  <form method="post" action="/oauth/consent">
    <input type="hidden" name="txn" value="{txn}">
    <label for="api_key">Your DevPulse API key</label>
    <input id="api_key" name="api_key" type="password" required autofocus
           autocomplete="off" spellcheck="false" placeholder="Paste your API key">
    <button type="submit">Authorize</button>
  </form>
  <p class="hint">
    Your API key was shown once when you created your account with
    <code>POST /users</code>. Approving grants this application the access
    listed above until you revoke it.
  </p>
</main>
</body>
</html>
"""

_ERROR_PAGE = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>DevPulse</title>
<style>
  body {{ font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 0;
         min-height: 100vh; display: grid; place-items: center; padding: 1.5rem;
         background: #f4f5f7; color: #16181d; }}
  main {{ max-width: 26rem; text-align: center; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e8eaee; }}
  }}
</style></head>
<body><main><h1>Cannot continue</h1><p>{message}</p></main></body>
</html>
"""


def _render(transaction: dict, txn: str, error: str | None = None) -> str:
    scopes = "".join(
        f"<li>{html.escape(SCOPE_DESCRIPTIONS.get(scope, scope))}</li>"
        for scope in transaction["scopes"]
    )
    return _PAGE.format(
        client=html.escape(str(transaction["client_name"])),
        scopes=scopes or "<li>Access your DevPulse account</li>",
        txn=html.escape(txn),
        error=f'<p class="error">{html.escape(error)}</p>' if error else "",
    )


def build_router(provider: DevPulseOAuthProvider) -> APIRouter:
    """Build the consent routes bound to a provider.

    A factory rather than a module-level router because the provider is
    constructed per application, and the tests need to bind their own.
    """
    router = APIRouter(prefix="/oauth", tags=["oauth"])

    @router.get("/consent", response_class=HTMLResponse)
    async def show_consent(txn: str) -> HTMLResponse:
        """Render the approval form for a parked authorization request."""
        try:
            transaction = await provider.describe_transaction(txn)
        except ConsentError as exc:
            return HTMLResponse(
                _ERROR_PAGE.format(message=html.escape(str(exc))),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return HTMLResponse(_render(transaction, txn))

    @router.post("/consent", response_model=None)
    async def submit_consent(
        txn: str = Form(...),
        api_key: str = Form(...),
    ) -> Response:
        """Verify the API key, then bounce back to the client with a code."""
        try:
            redirect_url = await provider.complete_consent(txn, api_key)
        except ConsentError as exc:
            # A wrong key is worth a retry; an expired request is not, and
            # describe_transaction will say so on the way back.
            try:
                transaction = await provider.describe_transaction(txn)
            except ConsentError as expired:
                return HTMLResponse(
                    _ERROR_PAGE.format(message=html.escape(str(expired))),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            return HTMLResponse(
                _render(transaction, txn, error=str(exc)),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return RedirectResponse(
            redirect_url, status_code=status.HTTP_303_SEE_OTHER
        )

    return router
