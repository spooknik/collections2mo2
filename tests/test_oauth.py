"""Tests for `oauth.py`: PKCE, the token dataclass, the credential store, the loopback
callback listener, the token endpoint calls and `BearerAuth`.

Nothing here talks to Nexus. `wait_for_code` is exercised against a *real* loopback
listener on a free port (the redirect handler is the one part of the flow a fake HTTP
layer would not test at all); everything else uses fakes.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.parse

import pytest
import requests

from collections2mo2 import oauth
from collections2mo2.nexus import ApiKeyAuth

# -- PKCE --------------------------------------------------------------------------------


def test_make_challenge_matches_rfc7636_appendix_b():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth.make_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_make_verifier_is_a_fresh_unreserved_string():
    a, b = oauth.make_verifier(), oauth.make_verifier()
    assert a != b
    for verifier in (a, b):
        assert 43 <= len(verifier) <= 128
        # RFC 7636 4.1: ALPHA / DIGIT / "-" / "." / "_" / "~"
        assert all(c.isalnum() or c in "-._~" for c in verifier), verifier
        # and it must survive the round trip through the challenge unchanged
        assert oauth.make_challenge(verifier)


def test_authorize_url_carries_every_pkce_parameter():
    url = oauth.authorize_url("challenge-value", "state-value", client_id="test-client")
    split = urllib.parse.urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == oauth.AUTHORIZE_URL
    params = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    assert params["client_id"] == "test-client"
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == oauth.REDIRECT_URI
    assert params["state"] == "state-value"
    assert params["code_challenge"] == "challenge-value"
    assert params["code_challenge_method"] == "S256"


def test_authorize_url_defaults_to_the_registered_client_id():
    params = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(oauth.authorize_url("c", "s")).query)
    )
    assert params["client_id"] == oauth.CLIENT_ID


# -- tokens ------------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    """An *unsigned* JWT: only the payload matters, `oauth` never verifies one."""

    def seg(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return ".".join(
        [
            seg(json.dumps({"alg": "none", "typ": "JWT"}).encode()),
            seg(json.dumps(payload).encode()),
            "signature",
        ]
    )


def test_tokens_from_response_uses_expires_in():
    tokens = oauth.Tokens.from_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, now=1000.0
    )
    assert tokens.access_token == "a"
    assert tokens.refresh_token == "r"
    assert tokens.expires_at == 4600.0
    assert tokens.token_type == "Bearer"


def test_tokens_from_response_falls_back_to_the_jwt_exp_claim():
    access = _jwt({"exp": 4600, "user": {"username": "Spooknik"}})
    tokens = oauth.Tokens.from_response({"access_token": access, "refresh_token": "r"})
    assert tokens.expires_at == 4600.0


def test_tokens_from_response_without_expiry_reads_as_already_expired():
    tokens = oauth.Tokens.from_response({"access_token": "not-a-jwt", "refresh_token": "r"})
    assert tokens.expires_at == 0.0
    assert tokens.expires_within(0.0) is True


def test_tokens_from_response_rejects_a_body_without_tokens():
    with pytest.raises(oauth.OAuthError):
        oauth.Tokens.from_response({"error": "invalid_grant"})


def test_tokens_json_round_trip():
    tokens = oauth.Tokens(
        access_token="a", refresh_token="r", expires_at=123.5, token_type="Bearer"
    )
    assert oauth.Tokens.from_json(tokens.to_json()) == tokens


def test_expires_within_uses_the_margin():
    tokens = oauth.Tokens(access_token="a", refresh_token="r", expires_at=1000.0)
    assert tokens.expires_within(300.0, now=699.0) is False
    assert tokens.expires_within(300.0, now=701.0) is True


def test_username_and_is_premium_come_from_the_unverified_claims():
    tokens = oauth.Tokens(
        access_token=_jwt({"user": {"username": "Spooknik", "membership_roles": ["premium"]}}),
        refresh_token="r",
        expires_at=0.0,
    )
    assert tokens.username == "Spooknik"
    assert tokens.is_premium is True


def test_is_premium_is_false_for_a_free_account_and_none_without_roles():
    free = oauth.Tokens(
        access_token=_jwt({"user": {"username": "Free", "membership_roles": ["member"]}}),
        refresh_token="r",
        expires_at=0.0,
    )
    assert free.is_premium is False
    bare = oauth.Tokens(
        access_token=_jwt({"user": {"username": "X"}}), refresh_token="r", expires_at=0.0
    )
    assert bare.is_premium is None
    assert bare.username == "X"


def test_claims_of_a_non_jwt_are_empty():
    tokens = oauth.Tokens(access_token="opaque-token", refresh_token="r", expires_at=0.0)
    assert tokens.claims == {}
    assert tokens.username is None
    assert tokens.is_premium is None


# -- TokenStore --------------------------------------------------------------------------


class _FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        del self.store[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(oauth.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(oauth.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(oauth.keyring, "delete_password", fake.delete_password)
    return fake


def test_token_store_round_trip(fake_keyring):
    store = oauth.TokenStore()
    assert store.load() is None

    tokens = oauth.Tokens(access_token="a", refresh_token="r", expires_at=99.0)
    store.save(tokens)
    assert store.load() == tokens

    store.clear()
    assert store.load() is None
    assert fake_keyring.store == {}


def test_token_store_splits_a_long_secret_into_chunks(fake_keyring):
    store = oauth.TokenStore()
    tokens = oauth.Tokens(access_token="A" * 2500, refresh_token="R" * 200, expires_at=1.0)
    store.save(tokens)

    count = int(fake_keyring.store[(store.service, store.username)])
    assert count >= 3
    assert all((store.service, f"{store.username}.{i}") in fake_keyring.store for i in range(count))
    assert store.load() == tokens


def test_token_store_drops_chunks_a_longer_token_left_behind(fake_keyring):
    store = oauth.TokenStore()
    store.save(oauth.Tokens(access_token="A" * 3000, refresh_token="r", expires_at=1.0))
    long_count = int(fake_keyring.store[(store.service, store.username)])

    short = oauth.Tokens(access_token="a", refresh_token="r", expires_at=2.0)
    store.save(short)
    short_count = int(fake_keyring.store[(store.service, store.username)])

    assert short_count < long_count
    stale = [(store.service, f"{store.username}.{i}") for i in range(short_count, long_count)]
    assert not any(key in fake_keyring.store for key in stale)
    assert store.load() == short


def test_token_store_reads_as_empty_when_a_chunk_is_missing(fake_keyring):
    store = oauth.TokenStore()
    store.save(oauth.Tokens(access_token="A" * 2500, refresh_token="r", expires_at=1.0))
    del fake_keyring.store[(store.service, f"{store.username}.1")]
    assert store.load() is None


def test_token_store_reads_as_empty_on_a_locked_credential_store(monkeypatch):
    def boom(*args, **kwargs):
        raise oauth.keyring.errors.KeyringError("locked")

    monkeypatch.setattr(oauth.keyring, "get_password", boom)
    assert oauth.TokenStore().load() is None


def test_token_store_save_failure_is_an_oauth_error(monkeypatch, fake_keyring):
    def boom(*args, **kwargs):
        raise oauth.keyring.errors.KeyringError("read only")

    monkeypatch.setattr(oauth.keyring, "set_password", boom)
    with pytest.raises(oauth.OAuthError):
        oauth.TokenStore().save(oauth.Tokens(access_token="a", refresh_token="r", expires_at=1.0))


# -- the loopback callback listener --------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _callback_url(port: int, **params) -> str:
    return f"http://127.0.0.1:{port}{oauth.REDIRECT_PATH}?{urllib.parse.urlencode(params)}"


def _run_wait_for_code(port: int, **kwargs) -> tuple[list, threading.Event]:
    """Start `wait_for_code` on a thread; returns its `[result_or_exception]` box and the
    event the listener sets once it is bound."""
    bound = threading.Event()
    box: list = []

    def run():
        try:
            box.append(oauth.wait_for_code(port=port, ready=bound.set, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - the test asserts on the type
            box.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert bound.wait(5), "the callback listener never bound its socket"
    return box, thread


def test_wait_for_code_returns_the_code_and_says_signed_in(monkeypatch):
    port = _free_port()
    box, thread = _run_wait_for_code(port, state="the-state", timeout=10)

    resp = requests.get(_callback_url(port, code="the-code", state="the-state"), timeout=5)

    thread.join(5)
    assert resp.status_code == 200
    assert "Signed in" in resp.text
    assert box == ["the-code"]


def test_wait_for_code_rejects_a_mismatched_state_and_keeps_waiting():
    port = _free_port()
    box, thread = _run_wait_for_code(port, state="the-state", timeout=10)

    bad = requests.get(_callback_url(port, code="nope", state="other-state"), timeout=5)
    assert bad.status_code == 400
    assert not box

    good = requests.get(_callback_url(port, code="the-code", state="the-state"), timeout=5)
    thread.join(5)
    assert good.status_code == 200
    assert box == ["the-code"]


def test_wait_for_code_raises_on_access_denied():
    port = _free_port()
    box, thread = _run_wait_for_code(port, state="the-state", timeout=10)

    resp = requests.get(_callback_url(port, error="access_denied", state="the-state"), timeout=5)
    thread.join(5)
    assert resp.status_code == 200
    assert len(box) == 1 and isinstance(box[0], oauth.OAuthError)
    assert "access_denied" in str(box[0])


def test_wait_for_code_honours_the_cancel_event():
    port = _free_port()
    cancel = threading.Event()
    box, thread = _run_wait_for_code(port, state="the-state", timeout=10, cancel=cancel)

    cancel.set()
    thread.join(5)
    assert len(box) == 1 and isinstance(box[0], oauth.LoginCancelled)


def test_wait_for_code_reports_a_port_already_in_use():
    port = _free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        with pytest.raises(oauth.OAuthError, match="could not listen"):
            oauth.wait_for_code("s", timeout=1, port=port)


# -- the token endpoint --------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _FakeSession:
    """Stands in for `requests.Session`: records every form posted, replies in order."""

    def __init__(self, *responses: _FakeResponse):
        self._responses = list(responses)
        self.posts: list[tuple[str, dict, dict]] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        return self._responses.pop(0)


def test_exchange_code_posts_the_verifier_and_returns_tokens():
    session = _FakeSession(
        _FakeResponse(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    )
    tokens = oauth.exchange_code("the-code", "the-verifier", client_id="cid", session=session)

    url, form, headers = session.posts[0]
    assert url == oauth.TOKEN_URL
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["code_verifier"] == "the-verifier"
    assert form["client_id"] == "cid"
    assert form["redirect_uri"] == oauth.REDIRECT_URI
    assert headers["Application-Name"] == "collections2mo2"
    assert tokens.access_token == "a"


def test_exchange_code_reports_a_server_error():
    session = _FakeSession(_FakeResponse(500, {"error_description": "boom"}))
    with pytest.raises(oauth.OAuthError, match="boom"):
        oauth.exchange_code("c", "v", session=session)


def test_refresh_posts_the_refresh_token():
    session = _FakeSession(
        _FakeResponse(200, {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600})
    )
    old = oauth.Tokens(access_token="a", refresh_token="r", expires_at=0.0)
    fresh = oauth.refresh(old, client_id="cid", session=session)

    _, form, _ = session.posts[0]
    assert form == {"grant_type": "refresh_token", "client_id": "cid", "refresh_token": "r"}
    assert (fresh.access_token, fresh.refresh_token) == ("a2", "r2")


def test_refresh_keeps_the_old_refresh_token_when_the_server_omits_it():
    session = _FakeSession(
        _FakeResponse(200, {"access_token": "a2", "refresh_token": "", "expires_in": 3600})
    )
    old = oauth.Tokens(access_token="a", refresh_token="keep-me", expires_at=0.0)
    assert oauth.refresh(old, session=session).refresh_token == "keep-me"


def test_a_rejected_refresh_is_signed_out():
    session = _FakeSession(_FakeResponse(400, {"error": "invalid_grant"}))
    old = oauth.Tokens(access_token="a", refresh_token="r", expires_at=0.0)
    with pytest.raises(oauth.SignedOut):
        oauth.refresh(old, session=session)


def test_signed_out_is_an_auth_required():
    from collections2mo2.nexus import AuthRequired

    assert issubclass(oauth.SignedOut, AuthRequired)
    assert issubclass(oauth.OAuthError, oauth.NexusError)


# -- BearerAuth ----------------------------------------------------------------------------


def _prepared(url: str) -> requests.PreparedRequest:
    return requests.Request("GET", url).prepare()


def test_bearer_auth_only_signs_api_requests():
    tokens = oauth.Tokens(access_token="tok", refresh_token="r", expires_at=time.time() + 3600)
    auth = oauth.BearerAuth(tokens)

    api_req = auth(_prepared("https://api.nexusmods.com/v1/users/validate.json"))
    assert api_req.headers["Authorization"] == "Bearer tok"

    cdn_req = auth(_prepared("https://cdn.nexusmods.com/skyrimspecialedition/mod.7z?token=x"))
    assert "Authorization" not in cdn_req.headers


def test_api_key_auth_only_signs_api_requests():
    auth = ApiKeyAuth("dev-key")
    assert (
        auth(_prepared("https://api.nexusmods.com/v1/games/x.json")).headers["apikey"] == "dev-key"
    )
    assert "apikey" not in auth(_prepared("https://supporter-files.nexus-cdn.com/x")).headers


def test_bearer_auth_refreshes_an_expiring_token_and_saves_it(fake_keyring):
    session = _FakeSession(
        _FakeResponse(200, {"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600})
    )
    store = oauth.TokenStore()
    stale = oauth.Tokens(
        access_token="stale",
        refresh_token="r",
        expires_at=time.time() + oauth.REFRESH_MARGIN - 10,
    )
    auth = oauth.BearerAuth(stale, store=store, session=session)

    req = auth(_prepared("https://api.nexusmods.com/v2/graphql"))

    assert req.headers["Authorization"] == "Bearer fresh"
    assert session.posts and session.posts[0][1]["grant_type"] == "refresh_token"
    assert store.load().access_token == "fresh"


def test_bearer_auth_does_not_refresh_a_fresh_token():
    session = _FakeSession()  # any call would IndexError
    tokens = oauth.Tokens(access_token="tok", refresh_token="r", expires_at=time.time() + 3600)
    auth = oauth.BearerAuth(tokens, session=session)
    assert auth.access_token() == "tok"
    assert session.posts == []


# -- default_auth ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_cached_auth(monkeypatch):
    """`default_auth` memoises the `BearerAuth` it built; never let one test's fake leak
    into the next, and never let `.env` from the checkout decide the outcome."""
    monkeypatch.setattr(oauth, "_cached_auth", None)
    monkeypatch.setattr(oauth, "load_dotenv", lambda *a, **kw: None)


def test_default_auth_prefers_the_environment_key(monkeypatch, fake_keyring):
    oauth.TokenStore().save(oauth.Tokens(access_token="a", refresh_token="r", expires_at=99.0))
    monkeypatch.setenv("NEXUS_API_KEY", "  dev-key  ")

    auth = oauth.default_auth()
    assert isinstance(auth, ApiKeyAuth)
    assert auth.api_key == "dev-key"


def test_default_auth_falls_back_to_the_stored_sign_in(monkeypatch, fake_keyring):
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    tokens = oauth.Tokens(access_token="a", refresh_token="r", expires_at=99.0)
    oauth.TokenStore().save(tokens)

    auth = oauth.default_auth()
    assert isinstance(auth, oauth.BearerAuth)
    assert auth.tokens == tokens


def test_default_auth_is_none_when_nobody_is_signed_in(monkeypatch, fake_keyring):
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    assert oauth.default_auth() is None


def test_default_auth_ignores_a_blank_environment_key(monkeypatch, fake_keyring):
    monkeypatch.setenv("NEXUS_API_KEY", "   ")
    assert oauth.default_auth() is None


def test_sign_out_clears_the_store_and_the_cache(monkeypatch, fake_keyring):
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    oauth.TokenStore().save(oauth.Tokens(access_token="a", refresh_token="r", expires_at=99.0))
    assert oauth.default_auth() is not None

    oauth.sign_out()

    assert fake_keyring.store == {}
    assert oauth.default_auth() is None


def test_saved_auth_is_none_without_a_stored_sign_in(fake_keyring):
    assert oauth.saved_auth() is None
