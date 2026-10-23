# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Copyright (C) 2026 Johan Pieterse
# Plain Sailing Information Systems
# Email: johan@plainsailingisystems.co.za
#
# This file is part of the AHG Archives MCP server.
#
# The AHG Archives MCP server is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the License,
# or (at your option) any later version.
#
# It is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with it. If not, see <https://www.gnu.org/licenses/>.

"""AHG Archives MCP server: AtoM catalogues for AI assistants, read and propose.

One service, no database of its own, two kinds of catalogue (GLAM_BACKEND):

  atom-ahg   AtoM with the AHG extensions (PSIS). Reads through REST API v2;
             proposals go into the existing review queue (AI > Review), where an
             archivist approves them into access points and dates.
  atom-base  Stock AtoM. Reads through AtoM's own REST API (arRestApiPlugin).
             Its write API overwrites rather than adds (one creator, one date),
             so proposals are returned for the archivist to apply; nothing is
             written. ponytail: an AtoM CSV for the import screen comes once its
             update mode is checked on a stock AtoM test site.

Every read runs with a key whose owner the catalogue's own rules apply to. With
no caller key that is the public service key (a user with no groups), so only
published, visible records come back. An archivist who sends their own key
(Authorization: Bearer <key>) reads and proposes as themselves, with their own
permissions. Proposals always need a caller key.

Every result carries the record's public URL and source, so an assistant can
cite it. Every tool call is logged as one JSON line.
"""

import json
import logging
import os
import time
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

def _setting(name: str, old: str = "", default: str | None = None) -> str:
    """A setting by its name, or by the name it had before (PSIS_*/GLAM_*). With
    no default it is required: an install that forgets it must not quietly use
    another catalogue's address."""
    value = os.environ.get(name) or (os.environ.get(old) if old else None) or default
    if value is None:
        raise SystemExit(f"{name} is required (see README, Install for your own catalogue)")
    return value


BACKEND = os.environ.get("GLAM_BACKEND", "atom-ahg")
PUBLIC_BASE = _setting("CATALOGUE_URL", "PSIS_PUBLIC_BASE").rstrip("/")
API_BASE = _setting("CATALOGUE_API_URL", "PSIS_API_BASE", PUBLIC_BASE + ("/api/v2" if BACKEND == "atom-ahg" else "/api")).rstrip("/")
API_KEY = _setting("CATALOGUE_API_KEY", "PSIS_API_KEY", "")
SOURCE = _setting("CATALOGUE_NAME", "GLAM_SOURCE_LABEL")
CALL_LOG = os.environ.get("GLAM_CALL_LOG", "")  # file path; empty means stdout
MAX_LIMIT = 25
ENTITY_TYPES = {"person": "PERSON", "organisation": "ORG", "place": "GPE", "date": "DATE"}

if BACKEND not in ("atom-ahg", "atom-base"):
    raise SystemExit(f"GLAM_BACKEND must be atom-ahg or atom-base, not {BACKEND!r}")

log = logging.getLogger("glam-mcp.calls")
log.setLevel(logging.INFO)
log.addHandler(logging.FileHandler(CALL_LOG) if CALL_LOG else logging.StreamHandler())

# GLAM_AUTH=oauth: the signed-in service (browser assistants sign in, desktop
# clients may send their catalogue key as a bearer). GLAM_AUTH=off: the public,
# read-only service, which never uses a caller key.
AUTH_MODE = os.environ.get("GLAM_AUTH", "off")
MCP_BASE = _setting("MCP_PUBLIC_URL", "GLAM_PUBLIC_BASE", "" if AUTH_MODE == "off" else None).rstrip("/")
HTTP_PATH = os.environ.get("GLAM_HTTP_PATH", "/mcp")
# Where to send people who try to propose on the public, read-only service.
SIGNED_IN_URL = _setting("MCP_SIGNED_IN_URL", default=(MCP_BASE + HTTP_PATH) if AUTH_MODE == "oauth" else "")
_auth_kwargs: dict = {}
_provider = None
if AUTH_MODE == "oauth":
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from glam_mcp.auth import CatalogueKeyChecker, CatalogueKeyOAuthProvider, Store
    _secret = _setting("MCP_SECRET", "GLAM_SECRET", "")
    if not _secret:
        raise SystemExit("GLAM_AUTH=oauth needs MCP_SECRET (a Fernet key)")
    _store = Store(os.path.join(os.environ.get("GLAM_DATA", "/data"), "oauth.db"), _secret)
    _checker = CatalogueKeyChecker(API_BASE, "X-API-Key" if BACKEND == "atom-ahg" else "REST-API-Key",
                                   "AHG-GLAM-MCP/0.3 (+https://www.atom-extensions.com)",
                                   "/descriptions?limit=1" if BACKEND == "atom-ahg" else "/informationobjects?limit=1")
    _provider = CatalogueKeyOAuthProvider(_store, _checker, MCP_BASE, SOURCE, PUBLIC_BASE + "/api/keys")
    _auth_kwargs = {
        "auth_server_provider": _provider,
        "auth": AuthSettings(
            issuer_url=MCP_BASE, resource_server_url=MCP_BASE + HTTP_PATH,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    }
elif AUTH_MODE != "off":
    raise SystemExit(f"GLAM_AUTH must be oauth or off, not {AUTH_MODE!r}")

mcp = FastMCP(
    "AHG Archives",
    instructions=(
        f"Search and read archival descriptions held in the {SOURCE}. Always cite the record URL "
        "given in each result, and say which institution the record comes from. To suggest access "
        "points (people, organisations, places) and dates for a record, read it with get_record, "
        "base every suggestion on its text (and, where its text is thin, on the file's text from get_record_text or what get_record_image shows), and call propose_access_points_and_dates; for text fields call "
        "propose_description_change. Give evidence and source URLs for everything, especially facts "
        "found on the web. Proposals are reviewed by an archivist; never describe them as saved."
    ),
    host=os.environ.get("GLAM_HOST", "0.0.0.0"),
    port=int(os.environ.get("GLAM_PORT", "8000")),
    streamable_http_path=HTTP_PATH,
    stateless_http=True,
    **_auth_kwargs,
)

if _provider is not None:
    mcp.custom_route("/login", methods=["GET"])(_provider.login_get)
    mcp.custom_route("/login", methods=["POST"])(_provider.login_post)

# PSIS's nginx drops requests from generic client user agents (bot protection),
# so the service names itself.
USER_AGENT = "AHG-GLAM-MCP/0.2 (+https://www.atom-extensions.com)"
KEY_HEADER = "X-API-Key" if BACKEND == "atom-ahg" else "REST-API-Key"
_client = httpx.Client(timeout=20.0, headers={"Accept": "application/json", "User-Agent": USER_AGENT})


class NotVisible(Exception):
    """The record does not exist or the caller may not see it. Both answer the same."""


class NeedsCallerKey(Exception):
    """A write was asked for without the archivist's own key."""


class Refused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _caller_key(ctx: Context | None) -> str | None:
    """The catalogue key of the signed-in user, or None on the public service.

    The bearer has already been checked by the auth middleware: it is either one
    of our OAuth tokens (mapped to the key the user gave at sign-in) or a
    catalogue key sent directly by a desktop client.
    """
    if _provider is None:
        return None
    from mcp.server.auth.middleware.auth_context import get_access_token
    return _provider.catalogue_key_for(get_access_token())


def _api(method: str, path: str, key: str | None = None, **kw) -> Any:
    r = _client.request(method, API_BASE + path, headers={KEY_HEADER: key or API_KEY}, **kw)
    if r.status_code == 404:
        raise NotVisible()
    if r.status_code in (401, 403):
        try:
            message = r.json().get("message") or "Not authorised"
        except Exception:
            message = "Not authorised"
        raise Refused(message)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) and "data" in body else body


def _logged(tool: str, args: dict, ctx: Context | None, fn):
    start = time.time()
    outcome, count = "ok", None
    try:
        result = fn()
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            count = len(result["results"])
        return result
    except NotVisible:
        outcome = "not_found"
        return {"found": False, "message": "No visible record with that reference.", "source": SOURCE}
    except NeedsCallerKey:
        outcome = "needs_key"
        return {"found": False, "message": "Proposing changes needs you to be signed in. Connect to " + (SIGNED_IN_URL or "this catalogue's signed-in MCP address") + " and sign in with your catalogue API key; the public read-only address cannot propose.", "source": SOURCE}
    except Refused as e:
        outcome = "refused"
        return {"found": False, "message": e.message, "source": SOURCE}
    except Exception as e:  # never show the caller internals
        outcome = "error:" + type(e).__name__
        return {"found": False, "message": "The catalogue could not be reached. Try again later.", "source": SOURCE}
    finally:
        log.info(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "args": args, "caller": "own-key" if _caller_key(ctx) else "public",
            "outcome": outcome, "results": count, "ms": int((time.time() - start) * 1000),
        }))


def _url(slug: str) -> str:
    return f"{PUBLIC_BASE}/{slug}"


def _file_url(path: str | None) -> str | None:
    return f"{PUBLIC_BASE}{path}" if path else None


def _limit(n: int) -> int:
    return max(1, min(int(n), MAX_LIMIT))


def _hit(slug: str, title: Any, level: Any) -> dict:
    return {"slug": slug, "title": title, "level": level, "url": _url(slug)}


# ── Reads ───────────────────────────────────────────────────────────────────

@mcp.tool()
def search_records(query: str, limit: int = 10, skip: int = 0, ctx: Context | None = None) -> dict:
    """Search archival descriptions (fonds, series, files, items) by keyword.

    Returns title, level of description and the citable URL of each match.
    Use get_record on a result for the full description.
    """
    key = _caller_key(ctx)

    def run():
        n, s = _limit(limit), max(0, int(skip))
        if BACKEND == "atom-ahg":
            data = _api("POST", "/search", key, json={"query": query, "limit": n, "skip": s})
            hits = [_hit(h["slug"], h.get("title"), h.get("level_of_description")) for h in data.get("results", []) if h.get("slug")]
        else:
            data = _api("GET", "/informationobjects", key, params={"sq0": query, "limit": n, "skip": s})
            hits = [_hit(h["slug"], h.get("title"), h.get("level_of_description")) for h in data.get("results", []) if h.get("slug")]
        return {"source": SOURCE, "total": data.get("total", len(hits)), "results": hits}
    return _logged("search_records", {"query": query, "limit": limit, "skip": skip}, ctx, run)


def _read_ahg(slug: str, key: str | None) -> dict:
    d = _api("GET", f"/descriptions/{slug}", key, params={"full": "true"})
    return {
        "found": True, "source": SOURCE, "url": _url(slug), "slug": slug,
        "title": d.get("title"), "reference_code": d.get("identifier"), "level": d.get("level_of_description"),
        "dates": d.get("dates"), "extent": d.get("extent_and_medium"), "scope_and_content": d.get("scope_and_content"),
        "archival_history": d.get("archival_history"), "names": d.get("names"), "subjects": d.get("subjects"),
        "places": d.get("places"), "notes": d.get("notes"),
        "part_of": [{"title": a.get("title"), "url": _url(a["slug"])} for a in d.get("hierarchy", []) if a.get("slug")],
        "children_count": d.get("children_count", 0),
        "provenance": d.get("provenance"),
        "images": [
            {"thumbnail": _file_url(o.get("thumbnail_url")), "reference": _file_url(o.get("reference_url")),
             "iiif_manifest": f"{PUBLIC_BASE}/iiif/manifest/{slug}"}
            for o in d.get("digital_objects", []) if o.get("reference_url") or o.get("thumbnail_url")
        ],
    }


def _read_base(slug: str, key: str | None) -> dict:
    # ponytail: field names follow arRestApiPlugin's read action; untested until
    # a stock AtoM test site is available.
    d = _api("GET", f"/informationobjects/{slug}", key)
    return {
        "found": True, "source": SOURCE, "url": _url(slug), "slug": slug,
        "title": d.get("title"), "reference_code": d.get("reference_code"), "level": d.get("level_of_description"),
        "dates": d.get("dates"), "extent": d.get("extent_and_medium"), "scope_and_content": d.get("scope_and_content"),
        "archival_history": d.get("archival_history"), "names": d.get("creators") or d.get("names"),
        "subjects": d.get("subject_access_points"), "places": d.get("place_access_points"), "notes": d.get("notes"),
        "part_of": [], "children_count": None, "images": [],
    }


@mcp.tool()
def get_record(slug: str, ctx: Context | None = None) -> dict:
    """Read one archival description in full, by its slug (the last part of its URL).

    Includes dates, names, subjects, places, notes and, where the catalogue
    provides them, its place in the hierarchy, its number of children, images,
    and its provenance chain (owners, dates, transfers, certainty, gaps).
    """
    key = _caller_key(ctx)
    return _logged("get_record", {"slug": slug}, ctx, lambda: (_read_ahg if BACKEND == "atom-ahg" else _read_base)(slug, key))


@mcp.tool()
def get_children(slug: str, limit: int = 25, skip: int = 0, ctx: Context | None = None) -> dict:
    """List the descriptions directly below one record (for example the series in a fonds)."""
    key = _caller_key(ctx)

    def run():
        if BACKEND == "atom-ahg":
            parent = _api("GET", f"/descriptions/{slug}", key)
            data = _api("GET", "/descriptions", key, params={"parent": parent["id"], "limit": _limit(limit), "skip": max(0, int(skip))})
            kids = [_hit(c["slug"], c.get("title"), c.get("level_of_description")) for c in data.get("results", []) if c.get("slug")]
            return {"source": SOURCE, "parent": {"title": parent.get("title"), "url": _url(slug)}, "total": data.get("total", 0), "results": kids}
        tree = _api("GET", f"/informationobjects/tree/{slug}", key)
        kids = [_hit(c["slug"], c.get("title"), c.get("level")) for c in (tree.get("children") or []) if c.get("slug")]
        start = max(0, int(skip))
        return {"source": SOURCE, "parent": {"title": tree.get("title"), "url": _url(slug)}, "total": len(kids), "results": kids[start:start + _limit(limit)]}
    return _logged("get_children", {"slug": slug, "limit": limit, "skip": skip}, ctx, run)


@mcp.tool()
def get_authority(slug: str, ctx: Context | None = None) -> dict:
    """Read an authority record (a person, family or organisation) by its slug."""
    key = _caller_key(ctx)

    def run():
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue's API does not publish authority records.", "source": SOURCE}
        a = _api("GET", f"/authorities/{slug}", key)
        return {
            "found": True, "source": SOURCE, "url": _url(slug), "name": a.get("authorized_form_of_name"),
            "entity_type": a.get("entity_type"), "dates_of_existence": a.get("dates_of_existence"),
            "history": a.get("history"), "places": a.get("places"), "functions": a.get("functions"),
        }
    return _logged("get_authority", {"slug": slug}, ctx, run)


@mcp.tool()
def get_iiif_manifest(slug: str, ctx: Context | None = None) -> dict:
    """Return the IIIF manifest URL for a record's images, for viewing in a IIIF viewer."""
    key = _caller_key(ctx)

    def run():
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue does not publish IIIF manifests.", "source": SOURCE}
        # Visibility is decided by the API first; the manifest is only offered for
        # records this key may read.
        rec = _read_ahg(slug, key)
        if not rec["images"]:
            return {"found": False, "message": "This record has no viewable images.", "source": SOURCE, "url": _url(slug)}
        manifest = f"{PUBLIC_BASE}/iiif/manifest/{slug}"
        return {"found": True, "source": SOURCE, "url": _url(slug), "title": rec["title"], "iiif_manifest": manifest}
    return _logged("get_iiif_manifest", {"slug": slug}, ctx, run)


@mcp.tool()
def get_record_text(slug: str, offset: int = 0, limit: int = 20000, ctx: Context | None = None) -> dict:
    """Read the text held for a record's files: text extracted from a PDF or by OCR,
    and transcriptions of audio or video.

    Use it for documents, where reading the text beats looking at a small image.
    Long text comes in pages: if a result says more=true, call again with
    offset set to offset + the length you received. Personal information may be
    masked. Quote what you rely on when you propose changes.
    """
    key = _caller_key(ctx)
    args = {"slug": slug, "offset": offset, "limit": limit}

    def run():
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue's API does not publish file text.", "source": SOURCE}
        d = _api("GET", f"/descriptions/{slug}/text", key, params={"start": max(0, int(offset)), "chars": max(1, min(int(limit), 50000))})
        texts = d.get("texts", [])
        if not texts:
            return {"found": True, "source": SOURCE, "url": _url(slug), "results": [],
                    "message": d.get("note") or "No text is held for this record's files. For an image, use get_record_image."}
        return {"found": True, "source": SOURCE, "url": _url(slug), "results": texts}
    return _logged("get_record_text", args, ctx, run)


MAX_IMAGE_BYTES = 3_000_000


@mcp.tool()
def get_record_image(slug: str, ctx: Context | None = None) -> list:
    """Look at a record's image: returns the reference image itself, so you can
    describe what it shows, read inscriptions or labels, or judge its condition.

    Use it before proposing a description for a record whose own text is thin.
    Only images the caller may view are returned (the reference copy, never the
    master). Say what you saw and what you inferred, and treat inscriptions you
    cannot read clearly as uncertain.
    """
    key = _caller_key(ctx)
    start = time.time()
    outcome, out = "ok", None
    try:
        if BACKEND != "atom-ahg":
            outcome = "unsupported"
            return ["This catalogue's API does not publish record images."]
        rec = _read_ahg(slug, key)  # the API decides whether this caller may see the record and its files
        ref = next((i.get("reference") or i.get("thumbnail") for i in rec["images"] if i.get("reference") or i.get("thumbnail")), None)
        if not ref:
            outcome = "no_image"
            return [f"{rec['title']} ({rec['url']}) has no image you may view."]
        r = _client.get(ref, headers={"Accept": "image/*"})
        r.raise_for_status()
        if len(r.content) > MAX_IMAGE_BYTES:
            outcome = "too_large"
            return [f"The image for {rec['title']} is too large to show here; open it at {ref}"]
        fmt = (r.headers.get("content-type", "image/jpeg").split("/")[-1].split(";")[0] or "jpeg").replace("jpg", "jpeg")
        out = len(r.content)
        return [json.dumps({"source": SOURCE, "url": rec["url"], "title": rec["title"], "image": ref}), Image(data=r.content, format=fmt)]
    except NotVisible:
        outcome = "not_found"
        return ["No visible record with that reference."]
    except Exception as e:
        outcome = "error:" + type(e).__name__
        return ["The image could not be fetched. Try again later."]
    finally:
        log.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": "get_record_image", "args": {"slug": slug},
                             "caller": "own-key" if key else "public", "outcome": outcome, "bytes": out,
                             "ms": int((time.time() - start) * 1000)}))


# ── Proposals ───────────────────────────────────────────────────────────────

class Proposal(BaseModel):
    kind: Literal["person", "organisation", "place", "date"]
    value: str = Field(description='As it should appear: a name, a place, or a date such as "1954" or "1938-1945".')
    evidence: str = Field("", description="Where in the record, or which source, supports it.")
    sources: list[str] = Field(default_factory=list, description="URLs of web sources used, if any.")
    dates_of_existence: str = Field("", description='Person or organisation only: life or existence dates for a new authority record, e.g. "1874-1949".')
    history: str = Field("", description="Person or organisation only: a short biographical or administrative history, from the record or a named source.")
    places: str = Field("", description="Person or organisation only: places associated with them, separated by semicolons.")


def _needs_key(ctx: Context | None) -> str:
    key = _caller_key(ctx)
    if not key:
        raise NeedsCallerKey()
    return key


@mcp.tool()
def propose_access_points_and_dates(slug: str, proposals: list[Proposal], ctx: Context | None = None) -> dict:
    """Propose access points (people, organisations, places) and dates for one record.

    Read the record with get_record first. Propose only what its text, or a
    reliable source you name, supports; give the evidence and source URLs for
    each. For a person or organisation, also give dates_of_existence, a short
    history and places when the record or a source states them: they pre-fill
    the new authority record if the archivist creates one. Nothing on the record
    changes: an archivist reviews every proposal.
    On AtoM with the AHG extensions they are queued in AI > NER review; on stock
    AtoM they are returned for the archivist to apply. Needs the archivist's own
    staff key.
    """
    args = {"slug": slug, "count": len(proposals)}

    def run():
        key = _needs_key(ctx)
        items = [p for p in proposals if p.value.strip()]
        if not items:
            return {"queued": [], "message": "Nothing to propose.", "source": SOURCE}
        if BACKEND == "atom-ahg":
            out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "entities": [
                {"type": ENTITY_TYPES[p.kind], "value": p.value.strip(), "evidence": p.evidence, "sources": p.sources,
                 **({"authority": a} if (a := {k: v.strip() for k, v in (("dates_of_existence", p.dates_of_existence), ("history", p.history), ("places", p.places)) if v.strip()})
                    and p.kind in ("person", "organisation") else {})} for p in items]})
            return {
                "source": SOURCE, "url": _url(slug), "queued": out.get("queued", []),
                "already_proposed_or_linked": out.get("skipped_existing", 0),
                "review_url": PUBLIC_BASE + "/ai/ner/review",
                "message": ("Queued for review. Nothing on the record changes until an archivist approves it."
                            if out.get("queued") else "Nothing new to queue: these were already proposed or linked for this record."),
            }
        _read_base(slug, key)  # the record exists and this key may read it
        return {
            "source": SOURCE, "url": _url(slug), "applied": False, "proposals": [p.model_dump() for p in items],
            "message": "This catalogue's API cannot add access points or dates without overwriting existing ones, "
                       "so nothing was saved. Apply these on the record's edit screen.",
        }
    return _logged("propose_access_points_and_dates", args, ctx, run)


@mcp.tool()
def propose_description_change(
    slug: str,
    field: Literal["title", "scope_and_content", "archival_history", "extent_and_medium", "arrangement", "access_conditions", "reproduction_conditions"],
    text: str,
    evidence: str = "",
    sources: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Propose new text for one field of a record, for an archivist to review.

    text is the complete proposed text for the field, not a fragment: read the
    record with get_record first and keep what is right in the current text.
    For a title, propose the title only (at most 1,024 characters).
    Give the evidence and the URLs of any web sources. Nothing on the record
    changes: on AtoM with the AHG extensions the proposal is queued in AI >
    Suggest review, where the archivist can edit it before approving; on stock
    AtoM it is returned for the archivist to apply. Needs the archivist's own
    staff key.
    """
    args = {"slug": slug, "field": field, "chars": len(text)}

    def run():
        key = _needs_key(ctx)
        if not text.strip():
            return {"queued": False, "message": "Nothing to propose.", "source": SOURCE}
        if BACKEND == "atom-ahg":
            out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "fields": [
                {"field": field, "text": text, "evidence": evidence, "sources": sources or []}]})
            queued = bool(out.get("fields_queued"))
            return {
                "source": SOURCE, "url": _url(slug), "field": field, "queued": queued,
                "review_url": PUBLIC_BASE + "/ai/suggest/review",
                "message": ("Queued for review. The archivist can edit it before approving; nothing changes until then."
                            if queued else "Nothing new to queue: the same text is already waiting for review."),
            }
        _read_base(slug, key)
        return {
            "source": SOURCE, "url": _url(slug), "applied": False, "field": field, "text": text, "evidence": evidence, "sources": sources or [],
            "message": "Nothing was saved. Apply this on the record's edit screen.",
        }
    return _logged("propose_description_change", args, ctx, run)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    try:
        r = _client.get(API_BASE + ("" if BACKEND == "atom-ahg" else "/informationobjects?limit=1"), headers={KEY_HEADER: API_KEY})
        ok = r.status_code < 500
    except Exception:
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "backend": BACKEND, "auth": AUTH_MODE, "source": SOURCE, "api": ok}, status_code=200 if ok else 503)


def main() -> None:
    if not API_KEY:
        raise SystemExit("PSIS_API_KEY is not set")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
