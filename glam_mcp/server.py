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

One service, no database of its own, two kinds of catalogue (GLAM_BACKEND; the
default, auto, asks the catalogue which API it has):

  atom-ahg   AtoM with the AHG extensions (PSIS). Reads through REST API v2;
             proposals go into the existing review queue (AI > Review), where an
             archivist approves them into access points and dates.
  atom-base  Stock AtoM. Reads through AtoM's own REST API (arRestApiPlugin).
             Its write API overwrites rather than adds (one creator, one date),
             so names and places come back as a one-row CSV for AtoM's own
             import screen; nothing is written.

Every read runs with a key whose owner the catalogue's own rules apply to. With
no caller key that is the public service key (a user with no groups), so only
published, visible records come back. An archivist who sends their own key
(Authorization: Bearer <key>) reads and proposes as themselves, with their own
permissions. Proposals always need a caller key.

Every result carries the record's public URL and source, so an assistant can
cite it. Every tool call is logged as one JSON line.

This module is the MCP layer: the tools, sign-in and the web routes. The rest:
  settings.py    settings, the call log, the catalogues and the current one (_c)
  catalogue.py   adapters: everything that talks to a catalogue
  calls.py       per-call policy: keys, the hourly proposal limit, the call log line
  keycheck.py    backend detection, the service-key check, public-address check
  auth.py        OAuth sign-in store and provider
  usage.py, catalogues.py   the staff pages /usage and /catalogues
"""


import functools
import json
import os
import time
from collections import defaultdict
from typing import Any, Literal

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from glam_mcp import calls
from glam_mcp import catalogue as adapters
from glam_mcp.calls import _budget, _caller_key, _logged, _needs_key, _read_key, _unknown_catalogue
from glam_mcp.catalogue import (
    _api,
    _fetch_image,
    _import_csv,
    _read_ahg,
    _read_base,
    _url,
    _visitor_sees,
    ImageRefused,
    NotVisible,
)
from glam_mcp.keycheck import BACKENDS, check as keycheck, detect_backend as _detect_backend
from glam_mcp.settings import (
    _c,
    _catalogues,
    _current,
    _setting,
    API_BASE,
    AUTH_MODE,
    BACKEND,
    CALL_LOG,
    CATALOGUES,
    ENTITY_TYPES,
    HOME,
    HTTP_PATH,
    log,
    MAX_LIMIT,
    MCP_BASE,
    PUBLIC_BASE,
    SOURCE,
    USER_AGENT,
)


_auth_kwargs: dict = {}
_provider = None
if AUTH_MODE == "oauth":
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from glam_mcp.auth import CatalogueKeyChecker, CatalogueKeyOAuthProvider, Store
    _secret = _setting("MCP_SECRET", "GLAM_SECRET", "")
    if not _secret:
        raise SystemExit("GLAM_AUTH=oauth needs MCP_SECRET (a Fernet key)")
    _store = Store(os.path.join(os.environ.get("GLAM_DATA", "/data"), "oauth.db"), _secret)
    _checker = CatalogueKeyChecker(API_BASE, BACKENDS[BACKEND][1],
                                   "AHG-GLAM-MCP/0.3 (+https://www.atom-extensions.com)",
                                   BACKENDS[BACKEND][2])
    _provider = CatalogueKeyOAuthProvider(_store, _checker, MCP_BASE, SOURCE, PUBLIC_BASE + ("/account/api-keys" if BACKEND == "openric" else "/api/keys"))
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
        + (" Other catalogues can be searched and read too (list_catalogues; search_records with catalogue=\"all\"): "
           "read each result with the catalogue it came from, and never propose changes to a record from another catalogue, "
           "since proposals go to the home catalogue only." if AUTH_MODE == "oauth" or os.environ.get("GLAM_OTHER_CATALOGUES", "[]").strip() not in ("", "[]") else "")
    ),
    host=os.environ.get("GLAM_HOST", "0.0.0.0"),
    port=int(os.environ.get("GLAM_PORT", "8000")),
    streamable_http_path=HTTP_PATH,
    stateless_http=True,
    **_auth_kwargs,
)

calls._provider = _provider  # whose key a call uses (calls._caller_key)

if _provider is not None:
    mcp.custom_route("/login", methods=["GET"])(_provider.login_get)
    mcp.custom_route("/login", methods=["POST"])(_provider.login_post)


def tool():
    """Register a tool that runs in a worker thread. The tools wait on catalogues
    over HTTP; run in the server's event loop they held every other caller up, so
    the service answered one call at a time (load test, 26 Oct 2026: 11 to 13 calls
    a second whether 1 or 25 clients asked). The function itself stays plain and
    synchronous, which is what the tests call. Context variables (the caller's
    sign-in, the current catalogue) are carried into the thread by anyio."""
    def register(fn):
        @functools.wraps(fn)
        async def in_thread(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        mcp.tool()(in_thread)
        return fn
    return register


def _limit(n: int) -> int:
    return max(1, min(int(n), MAX_LIMIT))


def _hit(slug: str, title: Any, level: Any) -> dict:
    return {"slug": slug, "title": title, "level": level, "url": _url(slug)}


# ── Reads ───────────────────────────────────────────────────────────────────

@tool()
def search_records(query: str, limit: int = 10, skip: int = 0, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Search archival descriptions (fonds, series, files, items) by keyword.

    Returns title, level of description and the citable URL of each match.
    Use get_record on a result for the full description. catalogue: leave empty
    for the home catalogue, give a name from list_catalogues, or "all" to search
    every catalogue at once (results grouped by catalogue). Every read tool takes
    the same catalogue; read a result with the catalogue it came from.
    """
    if catalogue == "all":
        return {"catalogues": [search_records(query, limit, skip, catalogue=name, ctx=ctx) for name in _catalogues()]}
    key = _read_key(ctx, catalogue)

    def run():
        n, s = _limit(limit), max(0, int(skip))
        if _c.backend == "atom-ahg":
            data = _api("POST", "/search", key, json={"query": query, "limit": n, "skip": s})
            hits = [_hit(h["slug"], h.get("title"), adapters.level_name(h.get("level_of_description")))
                    for h in data.get("results", []) if h.get("slug")]
        elif _c.backend == "openric":
            # Title search only; each hit is read for its citable page.
            hits = []
            for h in adapters.ric_search(query, n, s):
                try:
                    r = adapters._read_openric(str(h["id"]), key)
                except NotVisible:
                    continue
                hits.append({"slug": str(h["id"]), "title": r["title"] or h.get("label"), "level": r["level"], "url": r["url"]})
            data = {"total": s + len(hits) if len(hits) < n else None}
        else:
            data = _api("GET", "/informationobjects", key, params={"sq0": query, "limit": n, "skip": s})
            hits = [_hit(h["slug"], h.get("title"), h.get("level_of_description")) for h in data.get("results", [])
                    if h.get("slug") and _visitor_sees(h["slug"])]
            # The total counts records a visitor cannot see; report what is shown.
            data = {"total": s + len(hits) if len(hits) < n else data.get("total")}
        return {"source": _c.name, "total": data.get("total", len(hits)), "results": hits}
    return _logged("search_records", {"query": query, "limit": limit, "skip": skip, "catalogue": catalogue}, ctx, run)


@tool()
def list_catalogues(ctx: Context | None = None) -> dict:
    """The catalogues this service can search and read. Proposals go to the home catalogue only."""
    return _logged("list_catalogues", {}, ctx, lambda: {"catalogues": [
        {"name": c.name, "url": c.public_base, "home": c.home, "kind": BACKENDS[c.backend][3],
         "proposals": c.home and AUTH_MODE == "oauth", **({"demo": True} if c.demo else {})}
        for c in _catalogues().values()]})


def _read_record(slug: str, key: str | None) -> dict:
    """The record, read the way the current catalogue's backend reads it."""
    return {"atom-ahg": _read_ahg, "atom-base": _read_base, "openric": adapters._read_openric}[_c.backend](slug, key)


@tool()
def get_record(slug: str, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Read one archival description in full, by its slug (the last part of its URL).

    Includes dates, names, subjects, places, notes and, where the catalogue
    provides them, its place in the hierarchy, its number of children, images,
    and its provenance chain (owners, dates, transfers, certainty, gaps).
    """
    key = _read_key(ctx, catalogue)
    return _logged("get_record", {"slug": slug, "catalogue": catalogue}, ctx, lambda: _read_record(slug, key))


@tool()
def get_children(slug: str, limit: int = 25, skip: int = 0, catalogue: str = "", ctx: Context | None = None) -> dict:
    """List the descriptions directly below one record (for example the series in a fonds)."""
    key = _read_key(ctx, catalogue)

    def run():
        if _c.backend == "atom-ahg":
            parent = _api("GET", f"/descriptions/{slug}", key)
            data = _api("GET", "/descriptions", key, params={"parent": parent["id"], "limit": _limit(limit), "skip": max(0, int(skip))})
            kids = [_hit(c["slug"], c.get("title"), c.get("level_of_description")) for c in data.get("results", []) if c.get("slug")]
            return {"source": _c.name, "parent": {"title": parent.get("title"), "url": _url(slug)}, "total": data.get("total", 0), "results": kids}
        if _c.backend == "openric":
            rec, kids = adapters.ric_children(slug, key)
            start = max(0, int(skip))
            return {"source": _c.name, "parent": {"title": rec["title"], "url": rec["url"]}, "total": len(kids),
                    "results": kids[start:start + _limit(limit)]}
        tree = _api("GET", f"/informationobjects/tree/{slug}", key)
        if not _visitor_sees(slug):
            raise NotVisible()
        kids = [_hit(c["slug"], c.get("title"), c.get("level")) for c in (tree.get("children") or [])
                if c.get("slug") and _visitor_sees(c["slug"])]
        start = max(0, int(skip))
        return {"source": _c.name, "parent": {"title": tree.get("title"), "url": _url(slug)}, "total": len(kids), "results": kids[start:start + _limit(limit)]}
    return _logged("get_children", {"slug": slug, "limit": limit, "skip": skip, "catalogue": catalogue}, ctx, run)


@tool()
def work_through_series(slug: str, limit: int = 10, skip: int = 0, ctx: Context | None = None) -> dict:
    """Work through every record below a parent (for example each page of a family
    Bible), a batch at a time, with a running summary of what you proposed where.

    Returns a batch of the parent's children, each with your proposals on it so
    far by status, the totals for the whole batch, how many proposals you may
    still make this hour, and next_skip for the next batch (null when done).
    For each child: read it (get_record, and get_record_text or get_record_image
    when its text is thin), skip it when your proposals there are still pending,
    propose, then move on. Call again with skip=next_skip until it is null. When
    get_record shows a child has children of its own, work through it the same way.
    """
    key = _caller_key(ctx)

    def run():
        n, s = _limit(limit), max(0, int(skip))
        if _c.backend != "atom-ahg":
            # Stock AtoM keeps no proposals: the batch is the children alone.
            kids = get_children(slug, n, s, ctx=ctx)
            if "results" not in kids:
                return kids
            total = kids["total"]
            return {"source": _c.name, "parent": kids["parent"], "total_children": total,
                    "batch": [{**k, "my_proposals": {}} for k in kids["results"]], "batch_proposals_by_status": {},
                    "proposals_left_this_hour": _budget(key) if key else 0, "next_skip": s + n if s + n < total else None}
        parent = _api("GET", f"/descriptions/{slug}", key)
        data = _api("GET", "/descriptions", key, params={"parent": parent["id"], "limit": n, "skip": s})
        kids = [c for c in data.get("results", []) if c.get("slug")]
        mine: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        if key:
            for p in _api("GET", "/proposals", key, params={"mine": 1, "count": 200}).get("results", []):
                if p.get("slug"):
                    mine[p["slug"]][p.get("status") or "?"] += 1
        batch = [{**_hit(c["slug"], c.get("title"), c.get("level_of_description")),
                  "my_proposals": dict(mine.get(c["slug"], {}))} for c in kids]
        total = data.get("total", len(kids))
        totals: dict[str, int] = defaultdict(int)
        for b in batch:
            for st, k in b["my_proposals"].items():
                totals[st] += k
        return {
            "source": _c.name, "parent": {"title": parent.get("title"), "url": _url(slug)}, "total_children": total,
            "batch": batch, "batch_proposals_by_status": dict(totals),
            "proposals_left_this_hour": _budget(key) if key else 0,
            "next_skip": s + n if s + n < total else None,
            **({} if key else {"message": "Read-only here: proposing needs you to be signed in."}),
        }
    return _logged("work_through_series", {"slug": slug, "limit": limit, "skip": skip}, ctx, run)


@tool()
def get_authority(slug: str, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Read an authority record (a person, family or organisation) by its slug."""
    key = _read_key(ctx, catalogue)

    def run():
        if _c.backend == "openric":
            return adapters.ric_agent(slug, key)
        if _c.backend != "atom-ahg":
            return {"found": False, "error": "unsupported", "message": "This catalogue's API does not publish authority records.", "source": _c.name}
        a = _api("GET", f"/authorities/{slug}", key)
        return {
            "found": True, "source": _c.name, "url": _url(slug), "name": a.get("authorized_form_of_name"),
            "entity_type": a.get("entity_type"), "dates_of_existence": a.get("dates_of_existence"),
            "history": a.get("history"), "places": a.get("places"), "functions": a.get("functions"),
        }
    return _logged("get_authority", {"slug": slug, "catalogue": catalogue}, ctx, run)


PLACES_TAXONOMY_ID = 42
AUTHORITY_KINDS = {"person": "person", "organisation": "organization", "family": "family"}


@tool()
def search_authorities(name: str, kind: Literal["person", "organisation", "family", "place"] = "person",
                       limit: int = 10, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Find existing authority records (people, organisations, families) or places by name.

    Every word must appear in the name, in any order, so "Hendrik Engelbrecht"
    also finds "Engelbrecht, Hendrik". Use it before proposing a name or place:
    when one of the results is the same person, organisation or place, pass its
    id as existing_id in propose_access_points_and_dates, so the archivist links
    the existing record instead of creating a new one.
    """
    key = _read_key(ctx, catalogue)

    def run():
        n = _limit(limit)
        if _c.backend == "openric":
            hits = adapters.ric_search(name, n, 0, "Place" if kind == "place" else "Agent")
            return {"source": _c.name, "total": len(hits), "results": [{"id": h["id"], "name": h.get("label"), "kind": kind, "slug": str(h["id"])} for h in hits]}
        if _c.backend != "atom-ahg":
            return {"found": False, "error": "unsupported", "message": "This catalogue's API cannot search authority records.", "source": _c.name}
        if kind == "place":
            terms = _api("GET", f"/taxonomies/{PLACES_TAXONOMY_ID}/terms", key, params={"search": name}).get("terms", [])
            hits = [{"id": t["id"], "name": t.get("name"), "kind": "place"} for t in terms[:n]]
            return {"source": _c.name, "total": len(terms), "results": hits}
        data = _api("GET", "/authorities", key, params={"search": name, "entity_type": AUTHORITY_KINDS[kind], "limit": n})
        hits = [{"id": a["id"], "name": a.get("authorized_form_of_name"), "kind": kind, "slug": a.get("slug"),
                 "url": _url(a["slug"]) if a.get("slug") else None} for a in data.get("results", [])]
        return {"source": _c.name, "total": data.get("total", len(hits)), "results": hits}
    return _logged("search_authorities", {"name": name, "kind": kind, "limit": limit, "catalogue": catalogue}, ctx, run)


@tool()
def get_iiif_manifest(slug: str, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Return the IIIF manifest URL for a record's images, for viewing in a IIIF viewer."""
    key = _read_key(ctx, catalogue)

    def run():
        if _c.backend != "atom-ahg":
            return {"found": False, "error": "unsupported", "message": "This catalogue does not publish IIIF manifests.", "source": _c.name}
        # Visibility is decided by the API first; the manifest is only offered for
        # records this key may read.
        rec = _read_ahg(slug, key)
        if not rec["images"]:
            return {"found": False, "error": "no_image", "message": "This record has no viewable images.", "source": _c.name, "url": _url(slug)}
        manifest = f"{_c.public_base}/iiif/manifest/{slug}"
        return {"found": True, "source": _c.name, "url": _url(slug), "title": rec["title"], "iiif_manifest": manifest}
    return _logged("get_iiif_manifest", {"slug": slug, "catalogue": catalogue}, ctx, run)


@tool()
def get_record_text(slug: str, offset: int = 0, limit: int = 20000, catalogue: str = "", ctx: Context | None = None) -> dict:
    """Read the text held for a record's files: text extracted from a PDF or by OCR,
    and transcriptions of audio or video.

    Use it for documents, where reading the text beats looking at a small image.
    Long text comes in pages: if a result says more=true, call again with
    offset set to offset + the length you received. Personal information may be
    masked. Quote what you rely on when you propose changes.
    """
    key = _read_key(ctx, catalogue)
    args = {"slug": slug, "offset": offset, "limit": limit, "catalogue": catalogue}

    def run():
        if _c.backend == "openric" and adapters.ric_workspace(slug):
            return _ric_text(slug, key, max(0, int(offset)), max(1, min(int(limit), 50000)))
        if _c.backend != "atom-ahg":
            return {"found": False, "error": "unsupported", "message": "This catalogue's API does not publish file text.", "source": _c.name}
        d = _api("GET", f"/descriptions/{slug}/text", key, params={"start": max(0, int(offset)), "chars": max(1, min(int(limit), 50000))})
        texts = d.get("texts", [])
        if not texts:
            return {"found": True, "source": _c.name, "url": _url(slug), "results": [],
                    "message": d.get("note") or "No text is held for this record's files. For an image, use get_record_image."}
        return {"found": True, "source": _c.name, "url": _url(slug), "results": texts}
    return _logged("get_record_text", args, ctx, run)


def _ric_text(slug: str, key: str | None, offset: int, limit: int) -> dict:
    """OpenRiC Workspace: the published transcript of each online instantiation,
    in the same shape as the AHG API's file text."""
    d = adapters.ric_files(slug, key)
    url = f"{_c.public_base}/record/{slug}"
    texts = [{"source": "transcript", "digital_object_id": i.get("id"), "file": i.get("title"), "language": t.get("language"),
              "text": (t.get("value") or "")[offset:offset + limit], "more": offset + limit < len(t.get("value") or "")}
             for i in d.get("instantiations", []) for t in i.get("text", []) if t.get("value")]
    if not texts:
        return {"found": True, "source": _c.name, "url": url, "results": [],
                "message": "No published transcript is held for this record's files. For an image, use get_record_image."}
    return {"found": True, "source": _c.name, "url": url, "results": texts}


def _ric_image(slug: str, key: str | None) -> tuple[str, str, str | None]:
    """OpenRiC Workspace: (title, record url, reference or thumbnail image url). Masters are never listed."""
    d = adapters.ric_files(slug, key)
    files = [f for i in d.get("instantiations", []) for f in i.get("files", [])]
    pick = next((f for f in files if f.get("use") == "reference" and str(f.get("mime_type", "")).startswith("image/")), None) \
        or next((f for f in files if f.get("use") == "thumbnail" and str(f.get("mime_type", "")).startswith("image/")), None)
    return d.get("title") or slug, f"{_c.public_base}/record/{slug}", adapters._file_url(pick.get("path")) if pick else None


@tool()
def get_record_image(slug: str, catalogue: str = "", ctx: Context | None = None) -> list:
    """Look at a record's image: returns the reference image itself, so you can
    describe what it shows, read inscriptions or labels, or judge its condition.

    Use it before proposing a description for a record whose own text is thin.
    Only images the caller may view are returned (the reference copy, never the
    master). Say what you saw and what you inferred, and treat inscriptions you
    cannot read clearly as uncertain.
    """
    key = _read_key(ctx, catalogue)
    start = time.time()
    outcome, out = "ok", None
    cats = _catalogues()
    if (catalogue or HOME.name) not in cats:
        return [_unknown_catalogue(catalogue)["message"]]
    token = _current.set(cats[catalogue or HOME.name])
    try:
        if _c.backend == "openric" and adapters.ric_workspace(slug):
            title, url, ref = _ric_image(slug, key)  # Workspace answers 404 for a record that is not public
        elif _c.backend != "atom-ahg":
            outcome = "unsupported"
            return ["This catalogue's API does not publish record images."]
        else:
            rec = _read_ahg(slug, key)  # the API decides whether this caller may see the record and its files
            title, url = rec["title"], rec["url"]
            ref = next((i.get("reference") or i.get("thumbnail") for i in rec["images"] if i.get("reference") or i.get("thumbnail")), None)
        if not ref:
            outcome = "no_image"
            return [f"{title} ({url}) has no image you may view."]
        data, fmt = _fetch_image(ref)
        out = len(data)
        return [json.dumps({"source": _c.name, "url": url, "title": title, "image": ref}), Image(data=data, format=fmt)]
    except ImageRefused as e:
        outcome = e.outcome
        return [e.message]
    except NotVisible:
        outcome = "not_found"
        return ["No visible record with that reference."]
    except Exception as e:
        outcome = "error:" + type(e).__name__
        return ["The image could not be fetched. Try again later."]
    finally:
        log.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": "get_record_image", "args": {"slug": slug, "catalogue": catalogue},
                             "caller": "own-key" if key else "public", "outcome": outcome, "bytes": out,
                             "ms": int((time.time() - start) * 1000)}))
        _current.reset(token)


# ── Proposals ───────────────────────────────────────────────────────────────

def _queue_path(slug: str) -> str | None:
    """Where this catalogue takes proposals for a record, or None when it keeps
    no review queue (stock AtoM, the OpenRiC reference service)."""
    if _c.backend == "atom-ahg":
        return f"/descriptions/{slug}/proposals"
    if _c.backend == "openric" and adapters.ric_workspace(slug):
        return f"/records/{slug}/proposals"
    return None


def _review_url(ahg_path: str) -> str:
    # OpenRiC Workspace has one review queue for every kind (Manage > Proposals to review).
    return _c.public_base + ("/proposals" if _c.backend == "openric" else ahg_path)


class Proposal(BaseModel):
    kind: Literal["person", "organisation", "place", "date"]
    value: str = Field(description='As it should appear: a name, a place, or a date such as "1954" or "1938-1945".')
    evidence: str = Field("", description="Where in the record, or which source, supports it.")
    sources: list[str] = Field(default_factory=list, description="URLs of web sources used, if any.")
    dates_of_existence: str = Field("", description='Person or organisation only: life or existence dates for a new authority record, e.g. "1874-1949".')
    history: str = Field("", description="Person or organisation only: a short biographical or administrative history, from the record or a named source.")
    places: str = Field("", description="Person or organisation only: places associated with them, separated by semicolons.")
    existing_id: int | str | None = Field(None, description="Id from search_authorities of the existing record this most likely is (not for dates).")
    link_reason: str = Field("", description="With existing_id: why it is the same person, organisation or place, e.g. matching dates or places.")


@tool()
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
        if _c.backend not in ("atom-ahg", "openric"):
            return {"found": False, "error": "unsupported", "message": "This catalogue keeps no proposals: they are returned for the archivist to apply.", "source": _c.name}
        try:
            data = _api("GET", "/proposals", key, params={"mine": 1, "count": _limit(limit), "start": max(0, int(skip)), **({"status": status} if status else {})})
        except NotVisible:  # the OpenRiC reference service has no proposals route
            return {"found": False, "error": "unsupported", "message": "This catalogue keeps no proposals: they are returned for the archivist to apply.", "source": _c.name}
        # OpenRiC Workspace names the record by UUID ("record"); its page is /record/<uuid>.
        results = [{**r, "url": _url(r["slug"]) if r.get("slug") else (f"{_c.public_base}/record/{r['record']}" if r.get("record") else None)}
                   for r in data.get("results", [])]
        return {"source": _c.name, "total": data.get("total", len(results)), "results": results}
    return _logged("list_my_proposals", args, ctx, run)


@tool()
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
            return {"queued": [], "message": "Nothing to propose.", "source": _c.name}
        _budget(key, len(items))
        if path := _queue_path(slug):
            out = _api("POST", path, key, json={"source": "glam-mcp", "entities": [
                {"type": ENTITY_TYPES[p.kind], "value": p.value.strip(), "evidence": p.evidence, "sources": p.sources,
                 **({"authority": a} if (a := {k: v.strip() for k, v in (("dates_of_existence", p.dates_of_existence), ("history", p.history), ("places", p.places)) if v.strip()})
                    and p.kind in ("person", "organisation") else {}),
                 **({("term_id" if p.kind == "place" else "actor_id"): p.existing_id, "link_reason": p.link_reason}
                    if p.existing_id and p.kind != "date" else {})} for p in items]})
            return {
                "source": _c.name, "url": _url(slug), "queued": out.get("queued", []),
                "already_proposed_or_linked": out.get("skipped_existing", 0),
                "review_url": _review_url("/ai/ner/review"),
                "message": ("Queued for review. Nothing on the record changes until an archivist approves it."
                            if out.get("queued") else "Nothing new to queue: these were already proposed or linked for this record."),
            }
        if _c.backend == "openric":
            rec = adapters._read_openric(slug, key)
            return {"source": _c.name, "url": rec["url"], "applied": False, "proposals": [p.model_dump() for p in items],
                    "message": "This catalogue keeps no review queue: these are for an archivist to apply by hand."}
        rec = _read_base(slug, key)  # the record exists and a visitor may see it
        return {
            "source": _c.name, "url": _url(slug), "applied": False, "proposals": [p.model_dump() for p in items],
            **_import_csv(slug, rec, items, key),
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


# OpenRiC Workspace names relations by RiC type; each AtoM phrase above as Workspace
# reads it (its notation or its own wording, which also sets the direction). Not mapped,
# because RiC has no agent-to-agent equivalent: owner/owned, provider/client - Workspace
# then says so for that item.
RIC_RELATIONS = {
    "is the spouse of": "spouse", "is the sibling of": "sibling", "is the cousin of": "family",
    "is the parent of": "is the parent of", "is the child of": "is the child of",
    "is the grandparent of": "is an ancestor of", "is the grandchild of": "is a descendant of",
    "is the superior of": "has or had as a subordinate", "is the subordinate of": "is or was subordinate to",
    "controls": "controls or controlled", "is controlled by": "is or was controlled by",
    "is the predecessor of": "was succeeded by", "is the successor of": "succeeded",
    "is the business partner of": "work", "is the associate of": "agent-associated", "is the friend of": "knowing",
}


class Relationship(BaseModel):
    subject: str = Field(description="The first person or organisation, as proposed for this record or as named in the catalogue.")
    relation: RELATIONS = Field(description='How the subject relates to the object, read "subject relation object".')
    object: str = Field(description="The second person or organisation.")
    subject_existing_id: int | str | None = Field(None, description="Id from search_authorities when the subject already has an authority record.")
    object_existing_id: int | str | None = Field(None, description="Id from search_authorities when the object already has an authority record.")
    date: str = Field("", description='When it held, as written, e.g. "married 2 May 1898" or "1898-1949".')
    start_date: str = Field("", description="Start as YYYY-MM-DD, only when the exact day is known.")
    end_date: str = Field("", description="End as YYYY-MM-DD, only when the exact day is known.")
    evidence: str = Field("", description="The line in the record, or the source, that states it.")
    sources: list[str] = Field(default_factory=list, description="URLs of web sources used, if any.")


@tool()
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
        if not (path := _queue_path(slug)):
            rec = _read_record(slug, key)
            return {"source": _c.name, "url": rec["url"], "applied": False, "proposals": [r.model_dump() for r in relationships],
                    "message": "This catalogue's API cannot add relationships, so nothing was saved. Add them on the authority records' edit screens."}
        out = _api("POST", path, key, json={"source": "glam-mcp", "relationships": [
            {"subject_name": r.subject.strip(), "subject_actor_id": r.subject_existing_id,
             "relation": RIC_RELATIONS.get(r.relation, r.relation) if _c.backend == "openric" else r.relation,
             "object_name": r.object.strip(), "object_actor_id": r.object_existing_id, "date": r.date,
             "start_date": r.start_date, "end_date": r.end_date, "evidence": r.evidence, "sources": r.sources}
            for r in relationships]})
        results = out.get("relationships", [])
        return {
            "source": _c.name, "url": _url(slug), "results": results,
            "already_proposed": out.get("relationships_skipped_existing", 0),
            "review_url": out.get("relationship_review_url") or _review_url("/ai/relationship/review"),
            "message": ("Queued for review. Nothing changes until an archivist approves it."
                        if any(r.get("queued") for r in results) else "Nothing new was queued; see each result."),
        }
    return _logged("propose_relationships", args, ctx, run)


@tool()
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
            return {"queued": False, "message": "Nothing to propose.", "source": _c.name}
        _budget(key, 1)
        if path := _queue_path(slug):
            out = _api("POST", path, key, json={"source": "glam-mcp", "fields": [
                {"field": field, "text": text, "evidence": evidence, "sources": sources or []}]})
            queued = bool(out.get("fields_queued"))
            return {
                "source": _c.name, "url": _url(slug), "field": field, "queued": queued,
                "review_url": _review_url("/ai/suggest/review"),
                "message": ("Queued for review. The archivist can edit it before approving; nothing changes until then."
                            if queued else "Nothing new to queue: the same text is already waiting for review."),
            }
        rec = _read_record(slug, key)
        return {
            "source": _c.name, "url": rec["url"], "applied": False, "field": field, "text": text, "evidence": evidence, "sources": sources or [],
            "message": "Nothing was saved. Apply this on the record's edit screen.",
        }
    return _logged("propose_description_change", args, ctx, run)


@tool()
def propose_transcription(slug: str, text: str, digital_object_id: int | str | None = None, evidence: str = "",
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
            return {"queued": False, "message": "Nothing to propose.", "source": _c.name}
        _budget(key, 1)
        if not (path := _queue_path(slug)):
            rec = _read_record(slug, key)
            return {"source": _c.name, "url": rec["url"], "applied": False, "text": text,
                    "message": "This catalogue's API cannot store transcriptions, so nothing was saved."}
        if _c.backend == "openric":
            # Workspace publishes each page's transcript: do not propose one it already holds.
            norm = lambda t: " ".join(t.split())
            held = [i for i in adapters.ric_files(slug, key).get("instantiations", [])
                    if (digital_object_id is None or i.get("id") == digital_object_id)
                    and any(norm(t.get("value") or "") == norm(text) for t in i.get("text", []))]
            if held:
                return {"source": _c.name, "url": f"{_c.public_base}/record/{slug}", "queued": False,
                        "message": "This page already has that transcript, so nothing was queued."}
        out = _api("POST", path, key, json={"source": "glam-mcp", "transcripts": [
            {"text": text, "digital_object_id": digital_object_id, "evidence": evidence}]})
        queued = bool(out.get("fields_queued"))
        return {"source": _c.name, "url": _url(slug), "queued": queued, "review_url": _review_url("/ai/suggest/review"),
                "message": ("Queued for review beside the page image. Nothing changes until an archivist approves it."
                            if queued else "Nothing new to queue: the same transcription is already waiting for review.")}
    return _logged("propose_transcription", args, ctx, run)


@tool()
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
            return {"queued": False, "message": "Nothing to propose.", "source": _c.name}
        _budget(key, 1)
        if not (path := _queue_path(slug)):
            rec = _read_record(slug, key)
            return {"source": _c.name, "url": rec["url"], "applied": False, "field": field, "language": language, "text": text,
                    "message": "Nothing was saved. Add this translation on the record's edit screen in that language."}
        out = _api("POST", path, key, json={"source": "glam-mcp", "fields": [
            {"field": field, "culture": language.strip().lower(), "text": text, "evidence": evidence}]})
        queued = bool(out.get("fields_queued"))
        return {"source": _c.name, "url": _url(slug), "field": field, "language": language, "queued": queued,
                "review_url": _review_url("/ai/suggest/review"),
                "message": ("Queued for review. Nothing changes until an archivist approves it."
                            if queued else "Nothing new to queue: the same text is already waiting for review.")}
    return _logged("propose_translation", args, ctx, run)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    """ok only when the catalogue answers a real read with the service key;
    otherwise unreachable, key_refused or not_working, with 503."""
    try:
        r = adapters._client.get(_c.api_base + BACKENDS[_c.backend][2],
                                 headers={_c.key_header: _c.key})
        state = "ok" if r.status_code == 200 else "key_refused" if r.status_code in (401, 403) else "not_working"
    except Exception:
        state = "unreachable"
    return JSONResponse({"status": state, "backend": _c.backend, "auth": AUTH_MODE, "source": _c.name, "api": state == "ok"},
                        status_code=200 if state == "ok" else 503)


if AUTH_MODE == "oauth":
    # Staff usage figures (#10), on the signed-in service only; it reads both services' logs.
    from glam_mcp.usage import make_usage_route
    _logs = [p for p in os.environ.get("GLAM_USAGE_LOGS", CALL_LOG).split(",") if p]
    mcp.custom_route("/usage", methods=["GET", "POST"])(make_usage_route(_c.api_base, _c.key_header, USER_AGENT, _c.name, _logs))
    from glam_mcp.catalogues import make_catalogues_route
    mcp.custom_route("/catalogues", methods=["GET", "POST"])(make_catalogues_route(
        HOME.api_base, HOME.key_header, USER_AGENT, HOME.name, lambda: set(CATALOGUES), _detect_backend))


def main() -> None:
    # OpenRiC serves the public without a key (and refuses a key it does not know).
    if not _c.key and HOME.backend != "openric":
        raise SystemExit("CATALOGUE_API_KEY is not set")
    # The service key answers every caller without a key of their own, so it must
    # read no more than a visitor does. Refuse to start rather than serve with it.
    if why := keycheck(HOME.public_base, HOME.key, HOME.backend):
        raise SystemExit(f"CATALOGUE_API_KEY refused: {why}")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
