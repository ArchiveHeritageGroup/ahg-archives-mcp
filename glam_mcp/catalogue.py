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

"""Catalogue adapters: the HTTP client, reads for the atom-ahg and atom-base
backends, stock AtoM visibility and field filtering, image fetching and the
stock CSV import file. Everything that talks to a catalogue is here.
"""

import csv
import html
import io
import ipaddress
import re
import time
from collections import defaultdict
from typing import Any

import httpx

from glam_mcp.keycheck import not_public as _not_public
from glam_mcp.settings import _c, USER_AGENT


_client = httpx.Client(timeout=20.0, headers={"Accept": "application/json", "User-Agent": USER_AGENT})


class NotVisible(Exception):
    """The record does not exist or the caller may not see it. Both answer the same."""


class Refused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _off_network(r: httpx.Response) -> bool:
    """Demo catalogues only (added by staff on a web page): did this answer come
    from somewhere other than a public address? The name was checked when it
    was added, but it can point elsewhere by the time a connection is made
    (DNS rebinding), so this looks at the address actually connected to, before
    anything from it is used."""
    if not _c.demo:
        return False
    stream = r.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream else None
    # ponytail: a transport that reports no address (the unit tests' fake) passes.
    return bool(addr) and not ipaddress.ip_address(addr[0]).is_global


OFF_NETWORK = "This catalogue's address now leads to a non-public address, so it is not read."


def _api(method: str, path: str, key: str | None = None, **kw) -> Any:
    r = _client.request(method, _c.api_base + path, headers={_c.key_header: key or _c.key}, **kw)
    if _off_network(r):
        raise Refused(OFF_NETWORK)
    if r.status_code == 404:
        raise NotVisible()
    # 501: the catalogue lacks what this needs (e.g. AHG API without the AI plugin's review queues).
    # 429: the key's hourly or daily limit is used up (atom-ahg-plugins#223).
    if r.status_code in (401, 403, 429, 501):
        try:
            body = r.json()  # {message}, or RFC 7807 {title, detail} (OpenRiC)
            message = body.get("message") or body.get("detail") or body.get("title") or "Not authorised"
        except Exception:
            message = "Not authorised"
        raise Refused(message)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) and "data" in body else body


def _url(slug: str) -> str:
    return f"{_c.public_base}/{slug}"


def _file_url(path: str | None) -> str | None:
    """A file on the current catalogue's own site. Only a plain path is taken:
    anything that could name another host (a full address, //host, user@host)
    is dropped, whatever the catalogue's API returned."""
    if not path or not path.startswith("/") or path.startswith("//") or "@" in path or "\\" in path:
        return None
    return _c.public_base + path


def _read_ahg(slug: str, key: str | None) -> dict:
    d = _api("GET", f"/descriptions/{slug}", key, params={"full": "true"})
    return {
        "found": True, "source": _c.name, "url": _url(slug), "slug": slug,
        "title": d.get("title"), "reference_code": d.get("identifier"), "level": d.get("level_of_description"),
        "dates": d.get("dates"), "extent": d.get("extent_and_medium"), "scope_and_content": d.get("scope_and_content"),
        "archival_history": d.get("archival_history"), "names": d.get("names"), "subjects": d.get("subjects"),
        "places": d.get("places"), "notes": d.get("notes"),
        "part_of": [{"title": a.get("title"), "url": _url(a["slug"])} for a in d.get("hierarchy", []) if a.get("slug")],
        "children_count": d.get("children_count", 0),
        "provenance": d.get("provenance"),
        "images": [
            {"thumbnail": _file_url(o.get("thumbnail_url")), "reference": _file_url(o.get("reference_url")),
             "iiif_manifest": f"{_c.public_base}/iiif/manifest/{slug}"}
            for o in d.get("digital_objects", []) if o.get("reference_url") or o.get("thumbnail_url")
        ],
    }


_visible_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def _visitor_sees(slug: str) -> bool:
    """Stock AtoM only: may a visitor open this record's page? Its REST API checks
    only "read", never "view draft", so any key reads drafts (AtoM 2.10.2). The
    public page applies the catalogue's own rules (drafts, other denials) in any
    language, so a record counts as visible only when a visitor gets it.
    Anything but 200 is hidden. Remembered for five minutes."""
    now = time.time()
    hit = _visible_cache.get((_c.public_base, slug))
    if hit and hit[0] > now - 300:
        return hit[1]
    try:
        r = _client.get(f"{_c.public_base}/{slug}", headers={"Accept": "text/html"}, follow_redirects=False)
        ok = r.status_code == 200 and not _off_network(r)
    except httpx.HTTPError:
        ok = False
    if len(_visible_cache) > 5000:
        _visible_cache.clear()
    _visible_cache[(_c.public_base, slug)] = (now, ok)
    return ok


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _page_sections(slug: str) -> dict[str, str] | None:
    """Stock AtoM only: the record's public page as a visitor gets it, as
    {heading: the HTML under it until the next heading}; None when hidden.
    AtoM 2.10 shows each field as a heading (h3, or h4 inside an area)
    followed by its value."""
    try:
        r = _client.get(f"{_c.public_base}/{slug}", headers={"Accept": "text/html"}, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or _off_network(r):
        return None
    parts = re.split(r"<h([1-4])[^>]*>(.*?)</h\1>", r.text, flags=re.S)
    sections: dict[str, str] = {}
    for level, label, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        label = _clean(label)
        if level == "1":
            sections.setdefault("_heading", label)
        elif label:
            sections[label] = sections.get(label, "") + body
    return sections


def _text_under(sections: dict[str, str], *headings: str) -> str | None:
    return " ".join(t for t in (_clean(sections.get(h, "")) for h in headings) if t) or None


def _items_under(sections: dict[str, str], heading: str) -> list[str]:
    """The list items under one heading, without AtoM's role notes ("(Subject)")."""
    body = re.sub(r'<span class="note2">.*?</span>', "", sections.get(heading, ""), flags=re.S)
    return [t for t in (_clean(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", body, flags=re.S)) if t]


def _dates_under(sections: dict[str, str]) -> list[dict] | None:
    out = []
    for item in _items_under(sections, "Date(s)"):
        m = re.match(r"^(.*?)\s*\(([^()]*)\)$", item)
        out.append({"date": m.group(1), "type": m.group(2)} if m else {"date": item})
    return out or None


def _read_base(slug: str, key: str | None) -> dict:
    """Stock AtoM: the record as its public page shows it to a visitor. Stock
    AtoM's REST API returns fields the site hides (draft status unchecked,
    visible-elements settings, archivist's notes), so no field is taken from
    the API: every value here is what AtoM itself rendered for a visitor, after
    its own access rules. The API is used only to find records (search,
    children). ponytail: staff with their own key also see visitor fields here;
    full staff reads need the AHG API. Depends on AtoM 2.10's page layout,
    which tests/test_server.py fixes."""
    sections = _page_sections(slug)
    _visible_cache[(_c.public_base, slug)] = (time.time(), sections is not None)
    if sections is None:
        raise NotVisible()
    creators = re.findall(r'<div class="creator">\s*<a[^>]*>(.*?)</a>', sections.get("Name of creator", ""), flags=re.S)
    names = []
    for n in [_clean(c) for c in creators] + _items_under(sections, "Name access points"):
        if n and n not in names:
            names.append(n)
    return {
        "found": True, "source": _c.name, "url": _url(slug), "slug": slug,
        "title": _text_under(sections, "Title") or sections.get("_heading"),
        "reference_code": _text_under(sections, "Reference code"), "level": _text_under(sections, "Level of description"),
        "dates": _dates_under(sections), "extent": _text_under(sections, "Extent and medium"),
        "scope_and_content": _text_under(sections, "Scope and content"), "archival_history": _text_under(sections, "Archival history"),
        "names": names, "subjects": _items_under(sections, "Subject access points") or None,
        "places": _items_under(sections, "Place access points") or None,
        "notes": _text_under(sections, "Note", "Notes", "General note", "Archivist's note"),
        "repository": _text_under(sections, "Repository"), "part_of": [], "children_count": None, "images": [],
    }


# ── OpenRiC (RiC-O JSON-LD at /api/ric/v1, read only) ───────────────────────
# Records, agents and places come as JSON-LD with an @id that is the record's
# public page. OpenRiC has no full-text search (its title autocomplete is used),
# no file text and no review queue. A record's numeric id (from search) reads
# its places and related agents too.

def _ric_label(v: Any) -> str | None:
    """The text of a RiC-O value: a string, {"@value"}, or a node with a name or label."""
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        for k in ("@value", "rico:title", "rico:name", "skos:prefLabel", "rdfs:label", "rico:textualValue", "rico:expressedDate"):
            if v.get(k):
                return _ric_label(v[k])
    return None


def _ric_labels(v: Any) -> list[str]:
    items = v if isinstance(v, list) else [v] if v else []
    return [t for t in (_ric_label(i) for i in items) if t]


def _ric_ref(at_id: str | None) -> str | None:
    """The slug at the end of a record's @id (its public page)."""
    return at_id.rstrip("/").rsplit("/", 1)[-1] if at_id else None


def ric_search(query: str, n: int, s: int, types: str = "Record") -> list[dict]:
    """Title search through OpenRiC's autocomplete (it has no full-text search
    and no paging, so skip is applied here). Each hit: id, label, type."""
    hits = _api("GET", "/autocomplete", params={"q": query, "limit": min(s + n, 50)})
    hits = hits if isinstance(hits, list) else []
    return [h for h in hits if h.get("type") in types.split(",")][s:s + n]


def _read_openric(slug: str, key: str | None) -> dict:
    d = _api("GET", f"/records/{slug}", key)
    rid = slug if str(slug).isdigit() else None
    names, places = [], []
    if rid:  # related agents and places are read by numeric id only
        rel = _api("GET", f"/relations-for/{rid}", key)
        names = sorted({r["target_name"] for r in (rel.get("outgoing") or []) + (rel.get("incoming") or [])
                        if r.get("target_type") == "Agent" and r.get("target_name")})
        ents = _api("GET", f"/records/{rid}/entities", key, params={"types": "places"})
        places = _ric_labels(ents.get("places") if isinstance(ents, dict) else None)
    holder = d.get("rico:hasOrHadHolder")
    return {
        "found": True, "source": _c.name, "url": d.get("@id"), "slug": slug,
        "title": _ric_label(d.get("rico:title")), "reference_code": _ric_label(d.get("rico:identifier")),
        "level": _ric_label(d.get("rico:type")) or str(d.get("@type", "")).rsplit("#", 1)[-1],
        "dates": _ric_labels(d.get("rico:isAssociatedWithDate") or d.get("rico:date")) or None,
        "extent": None, "scope_and_content": _ric_label(d.get("rico:scopeAndContent")),
        "archival_history": _ric_label(d.get("rico:history")), "names": names,
        "subjects": _ric_labels(d.get("rico:hasOrHadSubject")) or None, "places": places or None, "notes": None,
        "repository": _ric_label(holder), "part_of": [], "children_count": None, "images": [],
    }


def ric_children(slug: str, key: str | None) -> tuple[dict, list[dict]]:
    """The record and the records directly below it, from OpenRiC's hierarchy walk."""
    rec = _read_openric(slug, key)
    if not str(slug).isdigit():
        return rec, []
    tree = _api("GET", f"/hierarchy/{slug}", key, params={"include": "children"})
    kids = []
    for c in (tree.get("children") or []) if isinstance(tree, dict) else []:
        cid = c.get("id") or c.get("entity_id")
        if cid:
            kids.append({"slug": str(cid), "title": _ric_label(c.get("title") or c.get("rico:title") or c.get("label")),
                         "level": _ric_label(c.get("level") or c.get("class")), "url": c.get("@id")})
    return rec, kids


def ric_agent(slug: str, key: str | None) -> dict:
    a = _api("GET", f"/agents/{slug}", key)
    return {
        "found": True, "source": _c.name, "url": a.get("@id"), "name": _ric_label(a.get("rico:name")),
        "entity_type": str(a.get("@type", "")).rsplit(":", 1)[-1], "dates_of_existence": _ric_label(a.get("rico:hasOrHadDateOfExistence")),
        "history": _ric_label(a.get("rico:history")), "places": None, "functions": None,
    }


MAX_IMAGE_BYTES = 3_000_000
IMAGE_TYPES = {"image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


class ImageRefused(Exception):
    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome, self.message = outcome, message


def _fetch_image(ref: str) -> tuple[bytes, str]:
    """The image at ref, which must be on the current catalogue's own host:
    no redirects, image types only, read in pieces and stopped at
    MAX_IMAGE_BYTES. For a demo catalogue (added by staff on a web page) the
    host must also still be a public address. ponytail: the address is resolved
    twice (check, then fetch); pin the address if demo catalogues go beyond staff."""
    u, home = httpx.URL(ref), httpx.URL(_c.public_base)
    if u.host != home.host or u.port != home.port or u.userinfo:
        raise ImageRefused("off_site", "The image is not on the catalogue's own site, so it is not fetched.")
    if _c.demo and (why := _not_public(ref)):
        raise ImageRefused("off_site", why)
    with _client.stream("GET", ref, headers={"Accept": "image/*"}, follow_redirects=False) as r:
        if _off_network(r):
            raise ImageRefused("off_site", OFF_NETWORK)
        if r.status_code != 200:
            raise ImageRefused("error:http", "The image could not be fetched. Try again later.")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in IMAGE_TYPES:
            raise ImageRefused("not_image", "The catalogue did not return an image for this record.")
        too_large = ImageRefused("too_large", f"The image is too large to show here; open it at {ref}")
        if int(r.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
            raise too_large
        data = bytearray()
        for chunk in r.iter_bytes():
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise too_large
    return bytes(data), IMAGE_TYPES[ctype]


# Stock AtoM's CSV import, in "update matches ignoring blank fields" mode, adds
# names, places and subjects to the matched record and leaves everything else
# alone (checked on AtoM 2.10.2). It replaces dates, and with them the creator
# links, so dates are never put in the file.
CSV_COLUMNS = {"person": "nameAccessPoints", "organisation": "nameAccessPoints", "place": "placeAccessPoints"}


def _import_csv(slug: str, rec: dict, items: list, key: str | None) -> dict:
    """Stock AtoM: the proposals as a one-row CSV for Import > CSV, matched on the
    record's identifier, title and repository (how AtoM finds it)."""
    identifier = (_api("GET", f"/informationobjects/tree/{slug}", key).get("identifier") or "").strip()
    dates = [p.value for p in items if p.kind == "date"]
    values: dict[str, list[str]] = defaultdict(list)
    for p in items:
        if p.kind in CSV_COLUMNS:
            v = p.value.strip().replace("|", "/")  # | separates values in AtoM CSV
            if v not in values[CSV_COLUMNS[p.kind]]:
                values[CSV_COLUMNS[p.kind]].append(v)
    note = (" Enter the dates by hand on the edit screen: " + "; ".join(dates) + ". An import would replace the record's dates.") if dates else ""
    if not values:
        return {"message": "Nothing was saved: this catalogue's API cannot add dates without overwriting them." + note}
    if not identifier or not rec.get("title") or not rec.get("repository"):
        return {"message": "Nothing was saved, and no import file could be made: AtoM matches an import on the record's "
                           "identifier, title and repository, and this record lacks " + ("an identifier" if not identifier else "a repository")
                           + ". Add the names and places on its edit screen." + note}
    out = io.StringIO()
    # Without publicationStatus an update import resets the record to the site's
    # default status (draft): checked on AtoM 2.10.2. Only records a visitor can
    # see get a file, so Published is their current status.
    cols = ["identifier", "title", "repository", "publicationStatus", *sorted(values)]
    w = csv.writer(out)
    w.writerow(cols)
    w.writerow([identifier, rec["title"], rec["repository"], "Published", *("|".join(values[c]) for c in sorted(values))])
    return {
        "import_csv": out.getvalue(),
        "message": "Nothing was saved yet. To apply the names and places without retyping, save import_csv as a .csv file and load it "
                   "in AtoM at Import > CSV: type Archival description, update behaviour \"Update matches ignoring blank fields in CSV\", "
                   "and tick \"Skip unmatched records\". AtoM adds them to the record and leaves the rest as it is." + note,
    }

