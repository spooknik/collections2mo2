"""Thin client for the Nexus Mods v2 GraphQL API and the collection download endpoints."""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import requests.auth

from . import __version__

API_BASE = "https://api.nexusmods.com"
API_HOST = "api.nexusmods.com"
GRAPHQL_URL = f"{API_BASE}/v2/graphql"
APPLICATION_NAME = "collections2mo2"
USER_AGENT = f"{APPLICATION_NAME}/{__version__} (+https://github.com/spooknik/collections2mo2)"
# The Acceptable Use Policy asks every request for a stable application name and the
# release it came from, on top of the User-Agent.
IDENTIFYING_HEADERS = {
    "User-Agent": USER_AGENT,
    "Application-Name": APPLICATION_NAME,
    "Application-Version": __version__,
}

COLLECTION_URL_RE = re.compile(
    r"^https?://(?:www\.)?nexusmods\.com/games/(?P<game>[a-z0-9_-]+)/collections/(?P<slug>[a-z0-9]+)",
    re.IGNORECASE,
)


class NexusError(RuntimeError):
    pass


class AuthRequired(NexusError):
    pass


SIGN_IN_HINT = "Sign in first (`c2mo2 login`, or the GUI's Sign in page)."
NOT_SIGNED_IN = (
    "Not signed in to Nexus Mods. Run `c2mo2 login` "
    "(developers: NEXUS_API_KEY in .env is allowed for testing only)."
)


def is_api_request(url: str | None) -> bool:
    """True for requests to the API host itself -- the only place credentials go.

    Download URLs the API hands out point at CDN hosts and are signed; sending the
    user's key or token there would leak it for nothing.
    """
    if not url:
        return False
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() == API_HOST
    except ValueError:
        return False


# Anything that can sit on `requests.Session.auth`: `ApiKeyAuth` (a developer's personal
# key from `NEXUS_API_KEY`, allowed by the policy for testing only) or
# `oauth.BearerAuth` (the OAuth sign-in every real user goes through).
NexusAuth = requests.auth.AuthBase


class ApiKeyAuth(requests.auth.AuthBase):
    """`apikey: <personal key>` on API requests. Testing use only, per the policy."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        if is_api_request(r.url):
            r.headers["apikey"] = self.api_key
        return r


@dataclass(frozen=True)
class CollectionRef:
    game: str
    slug: str

    @classmethod
    def parse(cls, url: str) -> CollectionRef:
        m = COLLECTION_URL_RE.match(url.strip())
        if not m:
            raise NexusError(f"not a Nexus collection URL: {url}")
        return cls(game=m.group("game").lower(), slug=m.group("slug").lower())


@dataclass(frozen=True)
class RevisionInfo:
    collection_id: int
    revision_id: int
    revision_number: int
    name: str
    game: str
    mod_count: int
    total_size: int
    download_link_path: str


class NexusClient:
    """One `requests.Session` against the Nexus API, authenticated by `auth`.

    `auth` is any `NexusAuth`; `api_key` is a shorthand for `ApiKeyAuth(api_key)` kept
    for tests and the `.env` developer override. With neither, the client can still read
    the anonymous GraphQL surface (collection metadata), and every authenticated call
    raises `AuthRequired` up front instead of a 401 from Nexus.
    """

    def __init__(
        self,
        auth: NexusAuth | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
    ):
        if api_key and auth is None:
            auth = ApiKeyAuth(api_key)
        self.auth = auth
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(IDENTIFYING_HEADERS)
        self.session.headers["Accept"] = "application/json"
        self.session.auth = auth

    @property
    def authenticated(self) -> bool:
        return self.auth is not None

    # -- GraphQL ---------------------------------------------------------------

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise AuthRequired("Nexus API rejected the request: authentication required")
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise NexusError(f"GraphQL errors: {body['errors']}")
        return body.get("data") or {}

    def revision_info(self, ref: CollectionRef, revision: int | None = None) -> RevisionInfo:
        """Resolve a collection slug (+ optional revision number) to ids and the download path.

        Without `revision`, the latest published revision is used.
        """
        query = """
        query($slug: String!, $revision: Int) {
          collection(slug: $slug, viewAdultContent: true) {
            id name game { domainName }
            latestPublishedRevision { revisionNumber }
          }
          collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true) {
            id revisionNumber modCount totalSize downloadLink
          }
        }
        """
        data = self.graphql(query, {"slug": ref.slug, "revision": revision})
        coll = data.get("collection")
        rev = data.get("collectionRevision")
        if not coll:
            raise NexusError(f"collection '{ref.slug}' not found")
        if not rev:
            raise NexusError(f"revision {revision} of '{ref.slug}' not found")
        if not rev.get("downloadLink"):
            raise NexusError("revision has no downloadLink (unpublished or retracted?)")
        return RevisionInfo(
            collection_id=int(coll["id"]),
            revision_id=int(rev["id"]),
            revision_number=int(rev["revisionNumber"]),
            name=coll["name"],
            game=(coll.get("game") or {}).get("domainName") or ref.game,
            mod_count=int(rev.get("modCount") or 0),
            total_size=int(rev.get("totalSize") or 0),
            download_link_path=rev["downloadLink"],
        )

    def latest_revision(self, ref: CollectionRef) -> int | None:
        """The newest published revision number, or `None` if the API does not say.

        One small GraphQL call, so `status` can ask it once per layer without pulling a
        whole revision record down.
        """
        query = """
        query($slug: String!) {
          collection(slug: $slug, viewAdultContent: true) {
            latestPublishedRevision { revisionNumber }
          }
        }
        """
        data = self.graphql(query, {"slug": ref.slug})
        coll = data.get("collection")
        if not coll:
            raise NexusError(f"collection '{ref.slug}' not found")
        latest = (coll.get("latestPublishedRevision") or {}).get("revisionNumber")
        return int(latest) if latest is not None else None

    def collection_revisions(self, ref: CollectionRef) -> list[dict[str, Any]]:
        """Every revision of a collection, newest first.

        Observed shape (2026-09): `{"revisionNumber", "revisionStatus", "modCount",
        "totalSize", "updatedAt"}`; `revisionStatus == "published"` is the only state a
        revision can actually be fetched in.
        """
        query = """
        query($slug: String!) {
          collection(slug: $slug, viewAdultContent: true) {
            revisions { revisionNumber revisionStatus modCount totalSize updatedAt }
          }
        }
        """
        data = self.graphql(query, {"slug": ref.slug})
        coll = data.get("collection")
        if not coll:
            raise NexusError(f"collection '{ref.slug}' not found")
        return list(coll.get("revisions") or [])

    def collection_changelog(
        self, ref: CollectionRef, revision: int | None
    ) -> dict[str, Any] | None:
        """The curator's changelog for a revision, or `None` when they wrote none.

        `CollectionRevision.collectionChangelog` carries `{id, collectionRevisionId,
        revisionNumber, description, createdAt, updatedAt}` (schema introspected
        2026-09); `description` is the free text shown on the collection page.
        """
        query = """
        query($slug: String!, $revision: Int) {
          collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true) {
            revisionNumber
            collectionChangelog { revisionNumber description createdAt }
          }
        }
        """
        data = self.graphql(query, {"slug": ref.slug, "revision": revision})
        rev = data.get("collectionRevision") or {}
        changelog = rev.get("collectionChangelog")
        return changelog if isinstance(changelog, dict) else None

    # -- Collection archive ---------------------------------------------------

    def collection_download_url(self, info: RevisionInfo) -> str:
        """Exchange the revision's download_link path for a real (time-limited) URL."""
        if not self.authenticated:
            raise AuthRequired(
                f"downloading a collection manifest needs a Nexus sign-in. {SIGN_IN_HINT}"
            )
        resp = self.session.get(API_BASE + info.download_link_path, timeout=self.timeout)
        if resp.status_code == 401:
            raise AuthRequired("Nexus rejected the sign-in (401)")
        resp.raise_for_status()
        body = resp.json()
        # Observed shape (2026-09): {"download_links": [{"name", "short_name", "URI"}, ...]}
        # one entry per CDN mirror; the first is Nexus's own CDN.
        links = body.get("download_links") if isinstance(body, dict) else None
        if links and isinstance(links, list) and links[0].get("URI"):
            return str(links[0]["URI"])
        raise NexusError(f"unexpected download_link response: {str(body)[:300]}")

    def download(self, url: str, dest: Path, chunk: int = 1 << 20) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, stream=True, timeout=self.timeout) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                fh.writelines(resp.iter_content(chunk_size=chunk))
        return dest

    # -- Mod files (v1 REST) ---------------------------------------------------

    def _get_v1_json(self, path: str, max_retries: int = 3) -> Any:
        """GET a v1 REST endpoint and return parsed JSON.

        Retries on HTTP 429 (honouring Retry-After, else 30s) and on 5xx / connection
        errors with exponential backoff (2s, 4s, 8s, ...), up to `max_retries` attempts.
        """
        if not self.authenticated:
            raise AuthRequired(f"the Nexus v1 API needs a sign-in. {SIGN_IN_HINT}")
        url = API_BASE + path
        attempt = 0
        while True:
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                attempt += 1
                if attempt > max_retries:
                    raise NexusError(f"connection error fetching {path}: {e}") from e
                time.sleep(2**attempt)
                continue
            if resp.status_code == 401:
                raise AuthRequired("Nexus rejected the sign-in (401)")
            if resp.status_code == 403:
                raise AuthRequired(
                    "Nexus rejected the request (403): a Premium account is required "
                    "for file downloads"
                )
            if resp.status_code == 429:
                attempt += 1
                if attempt > max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 30.0
                except ValueError:
                    delay = 30.0
                time.sleep(delay)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > max_retries:
                    resp.raise_for_status()
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()

    def game_info(self, domain: str) -> dict[str, Any]:
        """Fetch a game's v1 record. We use it for `categories` only.

        Observed shape (2026-09): {"id", "name", "domain_name", ...,
        "categories": [{"category_id", "name", "parent_category": <id>|false}, ...]};
        skyrimspecialedition reports 49 categories, which is exactly what MO2's
        "import Nexus categories" turns into `categories.dat`/`nexuscatmap.dat`.
        """
        body = self._get_v1_json(f"/v1/games/{domain}.json")
        if not isinstance(body, dict):
            raise NexusError(f"unexpected game info response: {str(body)[:300]}")
        return body

    def mod_file_info(self, domain: str, mod_id: int, file_id: int) -> dict[str, Any]:
        """Fetch file details for a specific mod file.

        Observed shape (2026-09): {"file_id", "name", "version", "category_id",
        "category_name", "size", "file_name", "uploaded_timestamp", "mod_version",
        "size_kb", "size_in_bytes", ...}.
        """
        body = self._get_v1_json(f"/v1/games/{domain}/mods/{mod_id}/files/{file_id}.json")
        if not isinstance(body, dict):
            raise NexusError(f"unexpected file info response: {str(body)[:300]}")
        return body

    def mod_file_download_url(self, domain: str, mod_id: int, file_id: int) -> str:
        """Exchange a mod file id for a real (time-limited) CDN URL.

        Premium-only endpoint (non-premium accounts get 403, surfaced as AuthRequired).
        Observed shape (2026-09): a JSON list of {"name", "short_name", "URI"} mirrors;
        the first entry is used.
        """
        body = self._get_v1_json(
            f"/v1/games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json"
        )
        if isinstance(body, list) and body and body[0].get("URI"):
            return str(body[0]["URI"])
        raise NexusError(f"unexpected download_link response: {str(body)[:300]}")
