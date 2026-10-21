"""GLAM MCP: AtoM for AI assistants, read and propose.

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
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

BACKEND = os.environ.get("GLAM_BACKEND", "atom-ahg")
PUBLIC_BASE = os.environ.get("PSIS_PUBLIC_BASE", "https://psis.theahg.co.za").rstrip("/")
API_BASE = os.environ.get("PSIS_API_BASE", PUBLIC_BASE + ("/api/v2" if BACKEND == "atom-ahg" else "/api")).rstrip("/")
API_KEY = os.environ.get("PSIS_API_KEY", "")
SOURCE = os.environ.get("GLAM_SOURCE_LABEL", "PSIS - AHG Extensions for AtoM demonstration catalogue")
CALL_LOG = os.environ.get("GLAM_CALL_LOG", "")  # file path; empty means stdout
MAX_LIMIT = 25
ENTITY_TYPES = {"person": "PERSON", "organisation": "ORG", "place": "GPE", "date": "DATE"}

if BACKEND not in ("atom-ahg", "atom-base"):
    raise SystemExit(f"GLAM_BACKEND must be atom-ahg or atom-base, not {BACKEND!r}")

log = logging.getLogger("glam-mcp.calls")
log.setLevel(logging.INFO)
log.addHandler(logging.FileHandler(CALL_LOG) if CALL_LOG else logging.StreamHandler())

mcp = FastMCP(
    "AHG Archives",
    instructions=(
        f"Search and read archival descriptions held in the {SOURCE}. Always cite the record URL "
        "given in each result, and say which institution the record comes from. To suggest access "
        "points (people, organisations, places) and dates for a record, read it with get_record, "
        "base every suggestion on its text, and call propose_access_points_and_dates. Proposals are "
        "reviewed by an archivist; never describe them as saved to the record."
    ),
    host=os.environ.get("GLAM_HOST", "0.0.0.0"),
    port=int(os.environ.get("GLAM_PORT", "8000")),
    stateless_http=True,
)

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
    """The archivist's own key, if their assistant sent one."""
    try:
        auth = ctx.request_context.request.headers.get("authorization", "")  # type: ignore[union-attr]
    except Exception:
        return None
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


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
        return {"found": False, "message": "Proposing changes needs your own staff API key, sent by your assistant as 'Authorization: Bearer <key>'.", "source": SOURCE}
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
    provides them, its place in the hierarchy, its number of children and images.
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


# ── Proposals ───────────────────────────────────────────────────────────────

@mcp.tool()
def propose_access_points_and_dates(
    slug: str,
    people: list[str] | None = None,
    organisations: list[str] | None = None,
    places: list[str] | None = None,
    dates: list[str] | None = None,
    evidence: str = "",
    ctx: Context | None = None,
) -> dict:
    """Propose access points and dates for one record, for an archivist to review.

    Read the record with get_record first and propose only what its text
    supports. people / organisations / places: names as they should appear as
    access points. dates: as written or normalised, e.g. "1942" or "1938-1945".
    evidence: a short note on where in the record each suggestion comes from.

    Nothing on the record changes. On AtoM with the AHG extensions the proposals
    are queued in AI > Review; on stock AtoM they are returned for the archivist
    to apply. Needs the archivist's own staff key.
    """
    key = _caller_key(ctx)
    groups = {"person": people, "organisation": organisations, "place": places, "date": dates}
    entities = [{"type": ENTITY_TYPES[k], "value": v.strip()} for k, vals in groups.items() for v in (vals or []) if v and v.strip()]
    args = {"slug": slug, "count": len(entities)}

    def run():
        if not key:
            raise NeedsCallerKey()
        if not entities:
            return {"queued": [], "message": "Nothing to propose.", "source": SOURCE}
        if BACKEND == "atom-ahg":
            out = _api("POST", f"/descriptions/{slug}/proposals", key, json={"entities": entities, "source": "glam-mcp"})
            return {
                "source": SOURCE, "url": _url(slug), "queued": out.get("queued", []),
                "already_proposed_or_linked": out.get("skipped_existing", 0),
                "review_url": PUBLIC_BASE + "/ai/ner/review",
                "message": ("Queued for review. Nothing on the record changes until an archivist approves it."
                            if out.get("queued") else "Nothing new to queue: these were already proposed or linked for this record."),
            }
        _read_base(slug, key)  # the record exists and this key may read it
        return {
            "source": SOURCE, "url": _url(slug), "applied": False, "proposals": entities, "evidence": evidence,
            "message": "This catalogue's API cannot add access points or dates without overwriting existing ones, "
                       "so nothing was saved. Apply these on the record's edit screen.",
        }
    return _logged("propose_access_points_and_dates", args, ctx, run)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    try:
        r = _client.get(API_BASE + ("" if BACKEND == "atom-ahg" else "/informationobjects?limit=1"), headers={KEY_HEADER: API_KEY})
        ok = r.status_code < 500
    except Exception:
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "backend": BACKEND, "source": SOURCE, "api": ok}, status_code=200 if ok else 503)


def main() -> None:
    if not API_KEY:
        raise SystemExit("PSIS_API_KEY is not set")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
