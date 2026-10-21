"""GLAM MCP, phase 1: read-only access to PSIS for AI assistants.

One service, no database of its own. Every read goes through the PSIS REST API
(v2) with a key whose owner has no groups, so the API's own rules decide what is
visible: published records only, nothing the access filter hides (embargo,
classification, donor restriction, ICIP, ODRL), POPIA field redaction applied.
The service never touches the PSIS database or file store (#218, heratio#1558).

Every result carries the record's public URL and the institution it comes from,
so an assistant can cite it. Every tool call is logged as one JSON line.
"""

import json
import logging
import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

API_BASE = os.environ.get("PSIS_API_BASE", "https://psis.theahg.co.za/api/v2").rstrip("/")
API_KEY = os.environ.get("PSIS_API_KEY", "")
PUBLIC_BASE = os.environ.get("PSIS_PUBLIC_BASE", "https://psis.theahg.co.za").rstrip("/")
SOURCE = os.environ.get("GLAM_SOURCE_LABEL", "PSIS - AHG Extensions for AtoM demonstration catalogue")
CALL_LOG = os.environ.get("GLAM_CALL_LOG", "")  # file path; empty means stdout
MAX_LIMIT = 25

log = logging.getLogger("glam-mcp.calls")
log.setLevel(logging.INFO)
log.addHandler(logging.FileHandler(CALL_LOG) if CALL_LOG else logging.StreamHandler())

mcp = FastMCP(
    "AHG Archives",
    instructions=(
        "Search and read archival descriptions, authority records and images held in the "
        f"{SOURCE}. Only published, publicly visible records are returned. Always cite the "
        "record URL given in each result, and say which institution the record comes from."
    ),
    host=os.environ.get("GLAM_HOST", "0.0.0.0"),
    port=int(os.environ.get("GLAM_PORT", "8000")),
    stateless_http=True,
)

# PSIS's nginx drops requests from generic client user agents (bot protection),
# so the service names itself.
USER_AGENT = "AHG-GLAM-MCP/0.1 (+https://www.atom-extensions.com)"
_client = httpx.Client(timeout=20.0, headers={"X-API-Key": API_KEY, "Accept": "application/json", "User-Agent": USER_AGENT})


class NotVisible(Exception):
    """The record does not exist or the caller may not see it. Both answer the same."""


def _api(method: str, path: str, **kw) -> Any:
    r = _client.request(method, API_BASE + path, **kw)
    if r.status_code == 404:
        raise NotVisible()
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _logged(tool: str, args: dict, fn):
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
    except Exception as e:  # never show the caller internals
        outcome = "error:" + type(e).__name__
        return {"found": False, "message": "The catalogue could not be reached. Try again later.", "source": SOURCE}
    finally:
        log.info(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "args": args,
            "outcome": outcome, "results": count, "ms": int((time.time() - start) * 1000),
        }))


def _url(slug: str) -> str:
    return f"{PUBLIC_BASE}/{slug}"


def _file_url(path: str | None) -> str | None:
    return f"{PUBLIC_BASE}{path}" if path else None


def _limit(n: int) -> int:
    return max(1, min(int(n), MAX_LIMIT))


@mcp.tool()
def search_records(query: str, limit: int = 10, skip: int = 0) -> dict:
    """Search archival descriptions (fonds, series, files, items) by keyword.

    Returns title, level of description and the citable URL of each match.
    Use get_record on a result for the full description.
    """
    args = {"query": query, "limit": limit, "skip": skip}

    def run():
        data = _api("POST", "/search", json={"query": query, "limit": _limit(limit), "skip": max(0, int(skip))})
        return {
            "source": SOURCE,
            "total": data.get("total", 0),
            "results": [
                {"slug": h["slug"], "title": h.get("title"), "level": h.get("level_of_description"), "url": _url(h["slug"])}
                for h in data.get("results", []) if h.get("slug")
            ],
        }
    return _logged("search_records", args, run)


@mcp.tool()
def get_record(slug: str) -> dict:
    """Read one archival description in full, by its slug (the last part of its URL).

    Includes dates, creators and other names, subjects, places, notes, its place in
    the hierarchy (the path up to the fonds), how many children it has, and any
    images the public may view.
    """
    def run():
        d = _api("GET", f"/descriptions/{slug}", params={"full": "true"})
        return {
            "found": True,
            "source": SOURCE,
            "url": _url(slug),
            "slug": slug,
            "title": d.get("title"),
            "reference_code": d.get("identifier"),
            "level": d.get("level_of_description"),
            "dates": d.get("dates"),
            "extent": d.get("extent_and_medium"),
            "scope_and_content": d.get("scope_and_content"),
            "names": d.get("names"),
            "subjects": d.get("subjects"),
            "places": d.get("places"),
            "notes": d.get("notes"),
            "part_of": [{"title": a.get("title"), "url": _url(a["slug"])} for a in d.get("hierarchy", []) if a.get("slug")],
            "children_count": d.get("children_count", 0),
            "images": [
                {"thumbnail": _file_url(o.get("thumbnail_url")), "reference": _file_url(o.get("reference_url")),
                 "iiif_manifest": f"{PUBLIC_BASE}/iiif/manifest/{slug}"}
                for o in d.get("digital_objects", []) if o.get("reference_url") or o.get("thumbnail_url")
            ],
        }
    return _logged("get_record", {"slug": slug}, run)


@mcp.tool()
def get_children(slug: str, limit: int = 25, skip: int = 0) -> dict:
    """List the descriptions directly below one record (for example the series in a fonds)."""
    args = {"slug": slug, "limit": limit, "skip": skip}

    def run():
        parent = _api("GET", f"/descriptions/{slug}")
        data = _api("GET", "/descriptions", params={"parent": parent["id"], "limit": _limit(limit), "skip": max(0, int(skip))})
        return {
            "source": SOURCE,
            "parent": {"title": parent.get("title"), "url": _url(slug)},
            "total": data.get("total", 0),
            "results": [
                {"slug": c["slug"], "title": c.get("title"), "level": c.get("level_of_description"), "url": _url(c["slug"])}
                for c in data.get("results", []) if c.get("slug")
            ],
        }
    return _logged("get_children", args, run)


@mcp.tool()
def get_authority(slug: str) -> dict:
    """Read an authority record (a person, family or organisation) by its slug."""
    def run():
        a = _api("GET", f"/authorities/{slug}")
        return {
            "found": True, "source": SOURCE, "url": _url(slug), "name": a.get("authorized_form_of_name"),
            "entity_type": a.get("entity_type"), "dates_of_existence": a.get("dates_of_existence"),
            "history": a.get("history"), "places": a.get("places"), "functions": a.get("functions"),
        }
    return _logged("get_authority", {"slug": slug}, run)


@mcp.tool()
def get_iiif_manifest(slug: str) -> dict:
    """Return the IIIF manifest URL and summary for a record's images, for viewing in a IIIF viewer."""
    def run():
        # Visibility is decided by the API first; the manifest is only offered for
        # records this key may read.
        d = _api("GET", f"/descriptions/{slug}", params={"full": "true"})
        if not any(o.get("reference_url") or o.get("thumbnail_url") for o in d.get("digital_objects", [])):
            return {"found": False, "message": "This record has no publicly viewable images.", "source": SOURCE, "url": _url(slug)}
        manifest = f"{PUBLIC_BASE}/iiif/manifest/{slug}"
        return {"found": True, "source": SOURCE, "url": _url(slug), "title": d.get("title"), "iiif_manifest": manifest,
                "viewer": f"https://projectmirador.org/embed/?iiif-content={manifest}"}
    return _logged("get_iiif_manifest", {"slug": slug}, run)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    try:
        r = _client.get(API_BASE)
        ok = r.status_code < 500
    except Exception:
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "source": SOURCE, "api": ok}, status_code=200 if ok else 503)


def main() -> None:
    if not API_KEY:
        raise SystemExit("PSIS_API_KEY is not set")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
