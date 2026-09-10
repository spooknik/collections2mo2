"""Nexus Mods sign-in: OAuth 2.0 with PKCE, as a public desktop client.

Nexus's API Acceptable Use Policy forbids a public application from taking a user's
*personal* API key (that key is for the user's own experiments only), so since 0.2.0
the tool signs users in the way Nexus asks of registered apps: OAuth 2.0 with Proof Key
for Code Exchange (https://modding.wiki/en/api/oauth2-guide). The sequence is

1. generate a random `code_verifier` and its SHA-256 `code_challenge`;
2. open `https://users.nexusmods.com/oauth/authorize?...` in the user's browser;
3. Nexus redirects the browser to `REDIRECT_URI` -- a loopback HTTP listener this
   module runs on `127.0.0.1` for the duration of the sign-in -- with a one-shot `code`;
4. POST the code + verifier to `https://users.nexusmods.com/oauth/token` for an access
   token (a JWT, ~6 h) and a refresh token.

Both the v1 REST API and the v2 GraphQL API accept the access token as
`Authorization: Bearer <token>`; `BearerAuth` attaches it to every request the
`NexusClient` session makes to `api.nexusmods.com` (and to nothing else -- CDN file
downloads are signed URLs and must not carry credentials), refreshing the token when it
is about to expire. Tokens live in the OS credential store through `keyring`
(`TokenStore`); the Windows Credential Manager caps a secret at 1280 UTF-16 characters
and a Nexus JWT plus refresh token is close to that, so the JSON is stored in chunks.

The JWT is *not* signature-verified here: the claims are only used for display (the
user name) and for the advisory "is this account Premium" hint -- every real decision is
made by Nexus when it accepts or rejects the token -- and verifying would pull in a JWT
library plus `cryptography` for a check that protects nothing on the client side.

`CLIENT_ID` is the id Nexus assigns when the app is registered (there is no self-service
registration; see docs/development.md). `C2MO2_NEXUS_CLIENT_ID` overrides it for
testing against a differently registered client. A developer can still bypass OAuth
with `NEXUS_API_KEY` in `.env` (`default_auth`); the policy allows a personal key for
testing, and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import keyring
import keyring.errors
import requests
from dotenv import load_dotenv

from . import __version__
from .nexus import USER_AGENT, ApiKeyAuth, AuthRequired, NexusAuth, NexusError, is_api_request

AUTHORIZE_URL = "https://users.nexusmods.com/oauth/authorize"
TOKEN_URL = "https://users.nexusmods.com/oauth/token"
# RFC 7009 revocation; `.well-known/openid-configuration` lists it (checked 2026-09-09).
REVOKE_URL = "https://users.nexusmods.com/oauth/revoke"
# Where a user can revoke the app's access to their account (Nexus's user service is a
# Doorkeeper deployment; the OAuth guide links "this page" without naming it).
AUTHORIZED_APPS_URL = "https://users.nexusmods.com/oauth/authorized_applications"

# Assigned by Nexus Mods when the application is registered. Until then this is the
# name we asked for; the authorize page will reject it.
CLIENT_ID = os.environ.get("C2MO2_NEXUS_CLIENT_ID") or "collections2mo2"
# The callback URI registered with the client id. A public desktop app has no web
# server, so it is a loopback listener; the port is fixed because the registered URI
# must match exactly.
REDIRECT_PORT = int(os.environ.get("C2MO2_OAUTH_PORT") or 43119)
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}{REDIRECT_PATH}"
# The guide's example requests no scopes; both APIs work on the bare token.
SCOPE = ""

# Refresh an access token this many seconds before Nexus would reject it, so a long
# download run never trips over expiry mid-request.
REFRESH_MARGIN = 300.0
# How long `login` waits for the user to press "Allow" in the browser.
LOGIN_TIMEOUT = 600.0

KEYRING_SERVICE = "collections2mo2"
KEYRING_USERNAME = "nexus-oauth"
# Windows Credential Manager: CRED_MAX_CREDENTIAL_BLOB_SIZE is 2560 bytes and pywin32
# writes the secret as UTF-16, i.e. 1280 characters; stay well under it per chunk.
_CHUNK_CHARS = 1000

_TOKEN_HEADERS = {
    "User-Agent": USER_AGENT,
    "Application-Name": "collections2mo2",
    "Application-Version": __version__,
    "Accept": "application/json",
}


class OAuthError(NexusError):
    """A sign-in step failed; the message is fit to show the user."""


class SignedOut(OAuthError, AuthRequired):
    """The refresh token was rejected: the user revoked the app (or the token aged out)
    and must sign in again. Raised from inside a request (`BearerAuth.__call__`), so it
    is also an `AuthRequired` for the engine's existing `except` clauses."""


class LoginCancelled(OAuthError):
    """The caller's cancel event was set while waiting for the browser."""


# -- tokens --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    # Absolute epoch seconds at which `access_token` stops being accepted.
    expires_at: float
    token_type: str = "Bearer"

    @classmethod
    def from_response(cls, body: dict[str, Any], *, now: float | None = None) -> Tokens:
        """Build from the token endpoint's JSON (`access_token`, `refresh_token`,
        `expires_in` seconds, `token_type`)."""
        try:
            access = str(body["access_token"])
            refresh = str(body["refresh_token"])
        except (KeyError, TypeError) as exc:
            raise OAuthError(f"unexpected token response: {str(body)[:200]}") from exc
        try:
            expires_in = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in <= 0:
            # Fall back to the JWT's own `exp` claim, then to "expired already" so the
            # first request refreshes and finds out.
            exp = _jwt_claims(access).get("exp")
            expires_at = float(exp) if isinstance(exp, (int, float)) else 0.0
        else:
            expires_at = (time.time() if now is None else now) + expires_in
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            token_type=str(body.get("token_type") or "Bearer"),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "token_type": self.token_type,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Tokens:
        data = json.loads(text)
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=float(data.get("expires_at") or 0.0),
            token_type=str(data.get("token_type") or "Bearer"),
        )

    def expires_within(self, seconds: float, *, now: float | None = None) -> bool:
        return (time.time() if now is None else now) + seconds >= self.expires_at

    # -- unverified claims, for display only ------------------------------------------

    @property
    def claims(self) -> dict[str, Any]:
        return _jwt_claims(self.access_token)

    @property
    def username(self) -> str | None:
        user = self.claims.get("user")
        name = user.get("username") if isinstance(user, dict) else None
        return str(name) if name else None

    @property
    def is_premium(self) -> bool | None:
        """`True`/`False` from the token's `membership_roles`, `None` if it has none."""
        user = self.claims.get("user")
        roles = user.get("membership_roles") if isinstance(user, dict) else None
        if not isinstance(roles, list):
            return None
        return any(str(r).lower() in ("premium", "lifetimepremium") for r in roles)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, **without** checking its signature. `{}` when `token` is
    not a JWT at all."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# -- storage ---------------------------------------------------------------------------


class TokenStore:
    """`Tokens` in the OS credential store, chunked to fit Windows's blob limit.

    Entries are `<KEYRING_USERNAME>` (the chunk count) and `<KEYRING_USERNAME>.<i>`.
    Every method swallows `KeyringError` into "nothing stored" / a raised `OAuthError`,
    because a locked-down credential store should read as signed out, not as a crash.
    """

    def __init__(self, service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME):
        self.service = service
        self.username = username

    def load(self) -> Tokens | None:
        try:
            count_text = keyring.get_password(self.service, self.username)
        except keyring.errors.KeyringError:
            return None
        if not count_text:
            return None
        try:
            count = int(count_text)
        except ValueError:
            return None
        chunks: list[str] = []
        for i in range(count):
            try:
                chunk = keyring.get_password(self.service, f"{self.username}.{i}")
            except keyring.errors.KeyringError:
                return None
            if chunk is None:
                return None
            chunks.append(chunk)
        try:
            return Tokens.from_json("".join(chunks))
        except (ValueError, KeyError, TypeError):
            return None

    def save(self, tokens: Tokens) -> None:
        text = tokens.to_json()
        chunks = [text[i : i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]
        try:
            old = keyring.get_password(self.service, self.username)
            for i, chunk in enumerate(chunks):
                keyring.set_password(self.service, f"{self.username}.{i}", chunk)
            keyring.set_password(self.service, self.username, str(len(chunks)))
            # Drop chunks a longer, older token pair left behind.
            for i in range(len(chunks), int(old) if old and old.isdigit() else 0):
                self._delete(f"{self.username}.{i}")
        except keyring.errors.KeyringError as exc:
            raise OAuthError(f"could not store the sign-in in the credential store: {exc}")

    def clear(self) -> None:
        try:
            old = keyring.get_password(self.service, self.username)
        except keyring.errors.KeyringError:
            old = None
        count = int(old) if old and old.isdigit() else 0
        # Sweep a few extra in case the count entry was lost.
        for i in range(max(count, 4)):
            self._delete(f"{self.username}.{i}")
        self._delete(self.username)

    def _delete(self, username: str) -> None:
        try:
            if keyring.get_password(self.service, username) is not None:
                keyring.delete_password(self.service, username)
        except keyring.errors.KeyringError:
            pass


# -- the PKCE dance ------------------------------------------------------------------


def make_verifier() -> str:
    # RFC 7636 4.1: 43..128 characters from the unreserved set. 64 random bytes,
    # base64url without padding, is 86.
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def make_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(challenge: str, state: str, *, client_id: str | None = None) -> str:
    params = {
        "client_id": client_id or CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


_CALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>collections2mo2</title>
<style>body{{font-family:system-ui,sans-serif;background:#1c1c1e;color:#eee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
div{{max-width:32rem;text-align:center}}</style></head>
<body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


class _Callback:
    """The one-shot loopback listener that receives the authorization code."""

    def __init__(self, state: str):
        self.state = state
        self.code: str | None = None
        self.error: str | None = None

    def handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the default stderr access log
                pass

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != REDIRECT_PATH:
                    self._reply(404, "Not found", "This is collections2mo2's sign-in listener.")
                    return
                query = urllib.parse.parse_qs(parsed.query)
                state = (query.get("state") or [""])[0]
                if state != outer.state:
                    self._reply(
                        400,
                        "Sign-in mismatch",
                        "This response does not belong to the current sign-in attempt. "
                        "Go back to collections2mo2 and try again.",
                    )
                    return
                if query.get("error"):
                    outer.error = (query.get("error_description") or query["error"])[0]
                    self._reply(
                        200,
                        "Sign-in cancelled",
                        "collections2mo2 was not given access. You can close this tab.",
                    )
                    return
                code = (query.get("code") or [""])[0]
                if not code:
                    outer.error = "Nexus Mods sent no authorization code"
                    self._reply(400, "Sign-in failed", "No authorization code was received.")
                    return
                outer.code = code
                self._reply(
                    200,
                    "Signed in",
                    "collections2mo2 is now connected to your Nexus Mods account. "
                    "You can close this tab and return to the app.",
                )

            def _reply(self, status: int, title: str, text: str):
                body = _CALLBACK_PAGE.format(title=title, text=text).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def wait_for_code(
    state: str,
    *,
    timeout: float = LOGIN_TIMEOUT,
    cancel: threading.Event | None = None,
    port: int = REDIRECT_PORT,
    ready: Callable[[], None] | None = None,
) -> str:
    """Listen on the loopback redirect port until the browser delivers the code.

    `ready` is called once the socket is bound, i.e. when it is safe to open the browser.
    """
    callback = _Callback(state)
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), callback.handler())
    except OSError as exc:
        raise OAuthError(
            f"could not listen on 127.0.0.1:{port} for the sign-in callback ({exc}). "
            "Another program (or another copy of collections2mo2) is using that port."
        ) from exc
    try:
        server.timeout = 0.25
        if ready is not None:
            ready()
        deadline = time.monotonic() + timeout
        while callback.code is None and callback.error is None:
            if cancel is not None and cancel.is_set():
                raise LoginCancelled("sign-in cancelled")
            if time.monotonic() >= deadline:
                raise OAuthError(
                    "timed out waiting for the browser sign-in; try again and press "
                    "Allow on the Nexus Mods page."
                )
            server.handle_request()
    finally:
        server.server_close()
    if callback.error:
        raise OAuthError(f"Nexus Mods did not authorise the app: {callback.error}")
    assert callback.code is not None
    return callback.code


def _post_token(form: dict[str, str], session: requests.Session | None) -> Tokens:
    sess = session or requests.Session()
    try:
        resp = sess.post(TOKEN_URL, data=form, headers=_TOKEN_HEADERS, timeout=30)
    except requests.RequestException as exc:
        raise OAuthError(f"could not reach Nexus Mods sign-in: {exc}") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            pass
        if 400 <= resp.status_code < 500 and form.get("grant_type") == "refresh_token":
            raise SignedOut(
                "Nexus Mods no longer accepts this sign-in"
                + (f" ({detail})" if detail else "")
                + "; sign in again."
            )
        raise OAuthError(
            f"Nexus Mods token request failed (HTTP {resp.status_code})"
            + (f": {detail}" if detail else "")
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError("Nexus Mods token response was not JSON") from exc
    return Tokens.from_response(body)


def exchange_code(
    code: str,
    verifier: str,
    *,
    client_id: str | None = None,
    session: requests.Session | None = None,
) -> Tokens:
    return _post_token(
        {
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "client_id": client_id or CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
        },
        session,
    )


def refresh(
    tokens: Tokens,
    *,
    client_id: str | None = None,
    session: requests.Session | None = None,
) -> Tokens:
    """A fresh token pair from `tokens.refresh_token`; raises `SignedOut` on 4xx."""
    fresh = _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id or CLIENT_ID,
            "refresh_token": tokens.refresh_token,
        },
        session,
    )
    if not fresh.refresh_token:
        # Servers that do not rotate refresh tokens omit it; keep the old one.
        fresh = replace(fresh, refresh_token=tokens.refresh_token)
    return fresh


def login(
    *,
    open_url: Callable[[str], Any] | None = None,
    timeout: float = LOGIN_TIMEOUT,
    cancel: threading.Event | None = None,
    store: TokenStore | None = None,
    client_id: str | None = None,
    session: requests.Session | None = None,
) -> Tokens:
    """Run the whole browser sign-in and persist the result.

    `open_url` receives the authorize URL once the callback listener is up (default:
    the system browser). `cancel` aborts the wait with `LoginCancelled`. Pass
    `store=None` to persist to the default `TokenStore`; a store that raises is a
    sign-in failure, because a token nobody can find again is worthless.
    """
    verifier = make_verifier()
    state = secrets.token_urlsafe(24)
    url = authorize_url(make_challenge(verifier), state, client_id=client_id)
    opener = open_url or webbrowser.open
    code = wait_for_code(state, timeout=timeout, cancel=cancel, ready=lambda: opener(url))
    tokens = exchange_code(code, verifier, client_id=client_id, session=session)
    store = store or TokenStore()
    store.save(tokens)
    _set_cached(BearerAuth(tokens, store=store, client_id=client_id))
    return tokens


# -- requests auth -------------------------------------------------------------------


class BearerAuth(requests.auth.AuthBase):
    """Attach `Authorization: Bearer <access token>` to Nexus API requests.

    Shared by every `NexusClient` in a run (the download stage builds one client per
    worker thread), so the refresh is serialised with a lock and the refreshed pair is
    written back to `store` for the next process. A rejected refresh raises `SignedOut`
    from inside the request, which callers see as `AuthRequired` via `NexusClient`.
    """

    def __init__(
        self,
        tokens: Tokens,
        *,
        store: TokenStore | None = None,
        client_id: str | None = None,
        session: requests.Session | None = None,
    ):
        self._tokens = tokens
        self._store = store
        self._client_id = client_id
        self._session = session
        self._lock = threading.Lock()

    @property
    def tokens(self) -> Tokens:
        with self._lock:
            return self._tokens

    def access_token(self) -> str:
        with self._lock:
            if self._tokens.expires_within(REFRESH_MARGIN):
                self._tokens = refresh(
                    self._tokens, client_id=self._client_id, session=self._session
                )
                if self._store is not None:
                    self._store.save(self._tokens)
            return self._tokens.access_token

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        if is_api_request(r.url):
            r.headers["Authorization"] = f"Bearer {self.access_token()}"
        return r


def saved_auth(store: TokenStore | None = None) -> BearerAuth | None:
    """A `BearerAuth` over the stored sign-in, or `None` when nobody is signed in."""
    store = store or TokenStore()
    tokens = store.load()
    if tokens is None:
        return None
    return BearerAuth(tokens, store=store)


# One `BearerAuth` per process: the GUI and the engine commands it runs must share the
# refresh lock, or two of them could refresh the same pair at once and (with refresh
# token rotation) the loser would be signed out.
_cache_lock = threading.Lock()
_cached_auth: BearerAuth | None = None


def default_auth() -> NexusAuth | None:
    """What the engine uses when a command does not hand it credentials explicitly.

    `NEXUS_API_KEY` (environment or `.env`) wins because it is the developer's
    testing override -- the one use of a personal key the Acceptable Use Policy allows;
    otherwise the OAuth sign-in in the credential store; otherwise `None`, which every
    command reports as "sign in first".
    """
    global _cached_auth
    load_dotenv()
    key = os.environ.get("NEXUS_API_KEY")
    if key and key.strip():
        return ApiKeyAuth(key.strip())
    with _cache_lock:
        if _cached_auth is None:
            _cached_auth = saved_auth()
        return _cached_auth


def _set_cached(auth: BearerAuth | None) -> None:
    global _cached_auth
    with _cache_lock:
        _cached_auth = auth


def revoke(
    tokens: Tokens, *, client_id: str | None = None, session: requests.Session | None = None
) -> bool:
    """Ask Nexus to invalidate the pair (RFC 7009). Best effort: `False` on any failure,
    because the local copy is deleted regardless and the user can also revoke on
    `AUTHORIZED_APPS_URL`."""
    sess = session or requests.Session()
    try:
        resp = sess.post(
            REVOKE_URL,
            data={
                "token": tokens.refresh_token or tokens.access_token,
                "token_type_hint": "refresh_token" if tokens.refresh_token else "access_token",
                "client_id": client_id or CLIENT_ID,
            },
            headers=_TOKEN_HEADERS,
            timeout=15,
        )
    except requests.RequestException:
        return False
    return resp.status_code < 400


def sign_out(store: TokenStore | None = None, *, revoke_remote: bool = True) -> None:
    """Forget the sign-in locally and (best effort) revoke it on Nexus."""
    store = store or TokenStore()
    tokens = store.load()
    store.clear()
    _set_cached(None)
    if tokens is not None and revoke_remote:
        revoke(tokens)
