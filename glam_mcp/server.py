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

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
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
# Proposal items one signed-in key may queue per hour (names, places, dates,
# relationships, texts), so one session cannot flood the review queues.
# ponytail: counted in this process only; a restart resets it. Move it to the
# catalogue (plugins #223, key daily caps) when keys are issued more widely.
PROPOSAL_CAP = int(os.environ.get("GLAM_PROPOSAL_CAP", "200"))
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
        "base every suggestion on its text (and, where its text is thin, on the file's text from get_record_text or what get_record_image shows), "
        "look each name and place up with search_authorities and give the id of the existing record it most likely is, "
        "check list_my_proposals so you do not repeat what an archivist already rejected, "
        "and call propose_access_points_and_dates; for relationships between people stated in the record "
        "(married to, child of), call propose_relationships after proposing both names; for a scanned page with no text "
        "call propose_transcription, and for text in another language propose_translation; for text fields call "
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
def work_through_series(slug: str, limit: int = 10, skip: int = 0, ctx: Context | None = None) -> dict:
    """Work through every record below a parent (for example each page of a family
    Bible), a batch at a time, with a running summary of what you proposed where.

    Returns a batch of the parent's children, each with your proposals on it so
    far by status, the totals for the whole batch, how many proposals you may
    still make this hour, and next_skip for the next batch (null when done).
    For each child: read it (get_record, and get_record_text or get_record_image
    when its text is thin), skip it when your proposals there are still pending,
    propose, then move on. Call again with skip=next_skip until it is null. A
    child with children of its own is a series: work through it the same way.
    """
    key = _caller_key(ctx)

    def run():
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue's API cannot list children with proposals; use get_children.", "source": SOURCE}
        n, s = _limit(limit), max(0, int(skip))
        parent = _api("GET", f"/descriptions/{slug}", key)
        data = _api("GET", "/descriptions", key, params={"parent": parent["id"], "limit": n, "skip": s})
        kids = [c for c in data.get("results", []) if c.get("slug")]
        mine: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        if key:
            for p in _api("GET", "/proposals", key, params={"mine": 1, "count": 200}).get("results", []):
                if p.get("slug"):
                    mine[p["slug"]][p.get("status") or "?"] += 1
        batch = [{**_hit(c["slug"], c.get("title"), c.get("level_of_description")),
                  "children_count": c.get("children_count"), "my_proposals": dict(mine.get(c["slug"], {}))} for c in kids]
        total = data.get("total", len(kids))
        totals: dict[str, int] = defaultdict(int)
        for b in batch:
            for st, k in b["my_proposals"].items():
                totals[st] += k
        return {
            "source": SOURCE, "parent": {"title": parent.get("title"), "url": _url(slug)}, "total_children": total,
            "batch": batch, "batch_proposals_by_status": dict(totals),
            "proposals_left_this_hour": _budget(key) if key else 0,
            "next_skip": s + n if s + n < total else None,
            **({} if key else {"message": "Read-only here: proposing needs you to be signed in."}),
        }
    return _logged("work_through_series", {"slug": slug, "limit": limit, "skip": skip}, ctx, run)


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


PLACES_TAXONOMY_ID = 42
AUTHORITY_KINDS = {"person": "person", "organisation": "organization", "family": "family"}


@mcp.tool()
def search_authorities(name: str, kind: Literal["person", "organisation", "family", "place"] = "person",
                       limit: int = 10, ctx: Context | None = None) -> dict:
    """Find existing authority records (people, organisations, families) or places by name.

    Every word must appear in the name, in any order, so "Hendrik Engelbrecht"
    also finds "Engelbrecht, Hendrik". Use it before proposing a name or place:
    when one of the results is the same person, organisation or place, pass its
    id as existing_id in propose_access_points_and_dates, so the archivist links
    the existing record instead of creating a new one.
    """
    key = _caller_key(ctx)

    def run():
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue's API cannot search authority records.", "source": SOURCE}
        n = _limit(limit)
        if kind == "place":
            terms = _api("GET", f"/taxonomies/{PLACES_TAXONOMY_ID}/terms", key, params={"search": name}).get("terms", [])
            hits = [{"id": t["id"], "name": t.get("name"), "kind": "place"} for t in terms[:n]]
            return {"source": SOURCE, "total": len(terms), "results": hits}
        data = _api("GET", "/authorities", key, params={"search": name, "entity_type": AUTHORITY_KINDS[kind], "limit": n})
        hits = [{"id": a["id"], "name": a.get("authorized_form_of_name"), "kind": kind, "slug": a.get("slug"),
                 "url": _url(a["slug"]) if a.get("slug") else None} for a in data.get("results", [])]
        return {"source": SOURCE, "total": data.get("total", len(hits)), "results": hits}
    return _logged("search_authorities", {"name": name, "kind": kind, "limit": limit}, ctx, run)


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
    existing_id: int | None = Field(None, description="Id from search_authorities of the existing record this most likely is (not for dates).")
    link_reason: str = Field("", description="With existing_id: why it is the same person, organisation or place, e.g. matching dates or places.")


def _needs_key(ctx: Context | None) -> str:
    key = _caller_key(ctx)
    if not key:
        raise NeedsCallerKey()
    return key


_spent: dict[str, deque] = defaultdict(deque)
_spent_lock = threading.Lock()


def _budget(key: str, n: int = 0) -> int:
    """Spend n proposal items for this key and return what is left this hour.
    Refuses, spending nothing, when n would go over the cap."""
    k = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    with _spent_lock:
        q = _spent[k]
        while q and q[0] < now - 3600:
            q.popleft()
        if n and len(q) + n > PROPOSAL_CAP:
            wait = int(q[0] + 3600 - now) // 60 + 1 if q else 60
            raise Refused(f"At most {PROPOSAL_CAP} proposals an hour per person; {PROPOSAL_CAP - len(q)} left. "
                          f"Try again in about {wait} minutes, or propose fewer at once.")
        q.extend([now] * n)
        return PROPOSAL_CAP - len(q)


@mcp.tool()
def list_my_proposals(status: Literal["pending", "approved", "edited", "linked", "rejected", "new_record_created", "parked"] | None = None,
                      limit: int = 25, skip: int = 0, ctx: Context | None = None) -> dict:
    """List the proposals you made and what became of them, newest first.

    Each shows the record, the name, place, date or field text proposed, and its
    status: pending, approved, edited, linked (to an authority record), rejected,
    new_record_created or parked, with who reviewed it and when. Check it before
    proposing for a record, and do not propose again what was rejected. Needs the
    archivist's own staff key.
    """
    args = {"status": status, "limit": limit, "skip": skip}

    def run():
        key = _needs_key(ctx)
        if BACKEND != "atom-ahg":
            return {"found": False, "message": "This catalogue keeps no proposals: they are returned for the archivist to apply.", "source": SOURCE}
        data = _api("GET", "/proposals", key, params={"mine": 1, "count": _limit(limit), "start": max(0, int(skip)), **({"status": status} if status else {})})
        results = [{**r, "url": _url(r["slug"]) if r.get("slug") else None} for r in data.get("results", [])]
        return {"source": SOURCE, "total": data.get("total", len(results)), "results": results}
    return _logged("list_my_proposals", args, ctx, run)


@mcp.tool()
def propose_access_points_and_dates(slug: str, proposals: list[Proposal], ctx: Context | None = None) -> dict:
    """Propose access points (people, organisations, places) and dates for one record.

    Read the record with get_record first. Propose only what its text, or a
    reliable source you name, supports; give the evidence and source URLs for
    each. For a person or organisation, also give dates_of_existence, a short
    history and places when the record or a source states them: they pre-fill
    the new authority record if the archivist creates one. Look names and places
    up with search_authorities first; when one is already in the catalogue, give
    its id as existing_id and why in link_reason, and the archivist sees it as
    the first candidate to link. Nothing on the record
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
        _budget(key, len(items))
        if BACKEND == "atom-ahg":
            out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "entities": [
                {"type": ENTITY_TYPES[p.kind], "value": p.value.strip(), "evidence": p.evidence, "sources": p.sources,
                 **({"authority": a} if (a := {k: v.strip() for k, v in (("dates_of_existence", p.dates_of_existence), ("history", p.history), ("places", p.places)) if v.strip()})
                    and p.kind in ("person", "organisation") else {}),
                 **({("term_id" if p.kind == "place" else "actor_id"): p.existing_id, "link_reason": p.link_reason}
                    if p.existing_id and p.kind != "date" else {})} for p in items]})
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


# The relation types stock AtoM installs (Actor relation type taxonomy); the API also
# accepts a catalogue's own terms by name.
RELATIONS = Literal[
    "is the spouse of", "is the parent of", "is the child of", "is the sibling of", "is the grandparent of",
    "is the grandchild of", "is the cousin of", "is the superior of", "is the subordinate of", "controls",
    "is controlled by", "is the owner of", "is owned by", "is the predecessor of", "is the successor of",
    "is the provider of", "is the client of", "is the business partner of", "is the associate of", "is the friend of",
]


class Relationship(BaseModel):
    subject: str = Field(description="The first person or organisation, as proposed for this record or as named in the catalogue.")
    relation: RELATIONS = Field(description='How the subject relates to the object, read "subject relation object".')
    object: str = Field(description="The second person or organisation.")
    subject_existing_id: int | None = Field(None, description="Id from search_authorities when the subject already has an authority record.")
    object_existing_id: int | None = Field(None, description="Id from search_authorities when the object already has an authority record.")
    date: str = Field("", description='When it held, as written, e.g. "married 2 May 1898" or "1898-1949".')
    start_date: str = Field("", description="Start as YYYY-MM-DD, only when the exact day is known.")
    end_date: str = Field("", description="End as YYYY-MM-DD, only when the exact day is known.")
    evidence: str = Field("", description="The line in the record, or the source, that states it.")
    sources: list[str] = Field(default_factory=list, description="URLs of web sources used, if any.")


@mcp.tool()
def propose_relationships(slug: str, relationships: list[Relationship], ctx: Context | None = None) -> dict:
    """Propose relationships between people or organisations that a record states,
    such as a marriage or parent and child in a family register.

    Each side must be an existing authority record (give its id from
    search_authorities) or a person or organisation you have already proposed
    for this record with propose_access_points_and_dates. Give the line that
    states each relationship as evidence. An archivist reviews them in AI >
    Relationship review; approved ones show on both authority records. Needs
    the archivist's own staff key.
    """
    args = {"slug": slug, "count": len(relationships)}

    def run():
        key = _needs_key(ctx)
        _budget(key, len(relationships))
        if BACKEND != "atom-ahg":
            _read_base(slug, key)
            return {"source": SOURCE, "url": _url(slug), "applied": False, "proposals": [r.model_dump() for r in relationships],
                    "message": "This catalogue's API cannot add relationships, so nothing was saved. Add them on the authority records' edit screens."}
        out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "relationships": [
            {"subject_name": r.subject.strip(), "subject_actor_id": r.subject_existing_id, "relation": r.relation,
             "object_name": r.object.strip(), "object_actor_id": r.object_existing_id, "date": r.date,
             "start_date": r.start_date, "end_date": r.end_date, "evidence": r.evidence, "sources": r.sources}
            for r in relationships]})
        results = out.get("relationships", [])
        return {
            "source": SOURCE, "url": _url(slug), "results": results,
            "already_proposed": out.get("relationships_skipped_existing", 0),
            "review_url": out.get("relationship_review_url") or PUBLIC_BASE + "/ai/relationship/review",
            "message": ("Queued for review. Nothing changes until an archivist approves it."
                        if any(r.get("queued") for r in results) else "Nothing new was queued; see each result."),
        }
    return _logged("propose_relationships", args, ctx, run)


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
        _budget(key, 1)
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


@mcp.tool()
def propose_transcription(slug: str, text: str, digital_object_id: int | None = None, evidence: str = "",
                          ctx: Context | None = None) -> dict:
    """Propose a transcription of a record's file, such as a scanned page with no text.

    Look at the page with get_record_image first and transcribe what is written,
    line by line, in its own language and spelling; mark what you cannot read as
    [illegible] rather than guessing. Use get_record_text to see whether the file
    already has text. digital_object_id picks the file when a record has several.
    At most 65,000 bytes: propose a long document page by page. An archivist
    compares it with the page image in AI > Suggest review; once approved it is
    the file's text and is searchable. Needs the archivist's own staff key.
    """
    args = {"slug": slug, "chars": len(text), "digital_object_id": digital_object_id}

    def run():
        key = _needs_key(ctx)
        if not text.strip():
            return {"queued": False, "message": "Nothing to propose.", "source": SOURCE}
        _budget(key, 1)
        if BACKEND != "atom-ahg":
            _read_base(slug, key)
            return {"source": SOURCE, "url": _url(slug), "applied": False, "text": text,
                    "message": "This catalogue's API cannot store transcriptions, so nothing was saved."}
        out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "transcripts": [
            {"text": text, "digital_object_id": digital_object_id, "evidence": evidence}]})
        queued = bool(out.get("fields_queued"))
        return {"source": SOURCE, "url": _url(slug), "queued": queued, "review_url": PUBLIC_BASE + "/ai/suggest/review",
                "message": ("Queued for review beside the page image. Nothing changes until an archivist approves it."
                            if queued else "Nothing new to queue: the same transcription is already waiting for review.")}
    return _logged("propose_transcription", args, ctx, run)


@mcp.tool()
def propose_translation(
    slug: str,
    field: Literal["title", "scope_and_content", "archival_history", "extent_and_medium", "arrangement", "access_conditions", "reproduction_conditions"],
    language: str,
    text: str,
    evidence: str = "",
    ctx: Context | None = None,
) -> dict:
    """Propose a translation of one field of a record into another language.

    language is the target language code, e.g. "en", "af" or "nl". Translate the
    record's current text for that field faithfully (read it with get_record);
    keep names, dates and reference codes as they are. An archivist reviews it in
    AI > Suggest review next to any text already held in that language; once
    approved the record shows it in that language. Needs the archivist's own
    staff key.
    """
    args = {"slug": slug, "field": field, "language": language, "chars": len(text)}

    def run():
        key = _needs_key(ctx)
        if not text.strip():
            return {"queued": False, "message": "Nothing to propose.", "source": SOURCE}
        _budget(key, 1)
        if BACKEND != "atom-ahg":
            _read_base(slug, key)
            return {"source": SOURCE, "url": _url(slug), "applied": False, "field": field, "language": language, "text": text,
                    "message": "Nothing was saved. Add this translation on the record's edit screen in that language."}
        out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"source": "glam-mcp", "fields": [
            {"field": field, "culture": language.strip().lower(), "text": text, "evidence": evidence}]})
        queued = bool(out.get("fields_queued"))
        return {"source": SOURCE, "url": _url(slug), "field": field, "language": language, "queued": queued,
                "review_url": PUBLIC_BASE + "/ai/suggest/review",
                "message": ("Queued for review. Nothing changes until an archivist approves it."
                            if queued else "Nothing new to queue: the same text is already waiting for review.")}
    return _logged("propose_translation", args, ctx, run)


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
