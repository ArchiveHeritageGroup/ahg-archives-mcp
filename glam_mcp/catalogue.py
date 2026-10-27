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
    """The catalogue (or this service) said no. code: refused, rate_limited or unsupported."""
    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.message, self.code = message, code


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
    k = key or _c.key
    r = _client.request(method, _c.api_base + path, headers={_c.key_header: k} if k else {}, **kw)
    if _off_network(r):
        raise Refused(OFF_NETWORK)
    if r.status_code == 404:
        raise NotVisible()
    # 501: the catalogue lacks what this needs (e.g. AHG API without the AI plugin's review queues).
    # 429: the key's hourly or daily limit is used up (atom-ahg-plugins#223).
    # 400: the catalogue refused the request and says why (OpenRiC: one malformed item refuses all).
    if r.status_code in (400, 401, 403, 429, 501):
        try:
            body = r.json()  # {message}, or RFC 7807 {title, detail} (OpenRiC)
            message = body.get("message") or body.get("detail") or body.get("title") or "Not authorised"
        except Exception:
            message = "Not authorised"
        raise Refused(message, {429: "rate_limited", 501: "unsupported"}.get(r.status_code, "refused"))
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) and "data" in body else body


_levels: dict[str, tuple[float, dict[int, str]]] = {}


def level_name(level: Any) -> Any:
    """AHG API search gives the level of description as a term id; its name comes
    from the level taxonomy (34), read with the service key and kept an hour.
    Anything that is not an id is returned as it is."""
    if not isinstance(level, int):
        return level
    hit = _levels.get(_c.api_base)
    if not hit or hit[0] < time.time() - 3600:
        try:
            terms = _api("GET", "/taxonomies/34/terms", _c.key).get("terms", [])
            hit = (time.time(), {t["id"]: t["name"] for t in terms if t.get("name")})
        except Exception:
            hit = (time.time() - 3300, hit[1] if hit else {})  # try again in five minutes
        _levels[_c.api_base] = hit
    return hit[1].get(level, level)


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


# AtoM's layout names the module and action on <body>; a description's own page is
# one of the description modules with the "index" action. A bot check, sign-in or
# error page answering 200 has no such class. ponytail: a custom theme that drops
# this class makes records unreadable (fails closed); widen the pattern for it.
_RECORD_PAGE = re.compile(r'<body[^>]*class="[^"]*\b(?:informationobject|sfIsadPlugin|sfRadPlugin|sfDcPlugin|sfModsPlugin|arDacsPlugin)\s+index\b')


class NotARecordPage(Refused):
    """The site answered 200 with something other than the record page (e.g. a bot check)."""

    def __init__(self):
        super().__init__("The catalogue's site answered with something other than the record page (for example a bot check), "
                         "so the record could not be read. Search and get_children still work; the site can let this "
                         "service's User-Agent past its check.", "unavailable")


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
        ok = r.status_code == 200 and not _off_network(r) and bool(_RECORD_PAGE.search(r.text))
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
    if not _RECORD_PAGE.search(r.text):
        raise NotARecordPage()
    crumbs = re.search(r'<ol class="breadcrumb">(.*?)</ol>', r.text, flags=re.S)
    parts = re.split(r"<h([1-4])[^>]*>(.*?)</h\1>", r.text, flags=re.S)
    sections: dict[str, str] = {}
    for level, label, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        label = _clean(label)
        if level == "1":
            sections.setdefault("_heading", label)
        elif label:
            sections[label] = sections.get(label, "") + body
    sections["_breadcrumb"] = crumbs.group(1) if crumbs else ""
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
        "repository": _text_under(sections, "Repository"), "part_of": _ancestors(sections), "children_count": _visible_children(slug, key),
        "images": [],
    }


def _ancestors(sections: dict[str, str]) -> list[dict]:
    """Stock AtoM: the records above this one, from the breadcrumb a visitor sees
    (top first), each kept only if a visitor may open it."""
    out = []
    for href, title in re.findall(r'<li class="breadcrumb-item">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', sections.get("_breadcrumb", ""), flags=re.S):
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if slug and _visitor_sees(slug):
            out.append({"title": _clean(title), "url": _url(slug)})
    return out


def tree_page(slug: str, skip: int, limit: int) -> tuple[list[dict], int]:
    """Stock AtoM: one page of the records directly below this one, as a visitor's
    treeview gets them (fullWidthTreeView): direct children only, paged, drafts
    left out for anyone not signed in, and the parent's own read check applied.
    Not the REST API's tree, which returns the whole subtree in one answer
    (12,091 records for one fonds took over a minute) and includes drafts.
    Returns (children, how many a visitor may see)."""
    r = _client.get(f"{_c.public_base}/{slug}/informationobject/fullWidthTreeView",
                    params={"nodeLimit": max(1, limit), "skip": max(0, skip)},
                    headers={"Accept": "application/json"}, follow_redirects=False)
    if r.status_code != 200 or _off_network(r):
        raise NotVisible()
    try:
        d = r.json()
    except ValueError:
        raise NotARecordPage()  # e.g. a bot check answering instead of AtoM
    kids = []
    for n in d.get("nodes") or []:
        a = n.get("a_attr") or {}
        kid = str(a.get("href", "")).rstrip("/").rsplit("/", 1)[-1]
        if kid:
            # AtoM labels each node "[Level] Title".
            kids.append({"slug": kid, "title": re.sub(r"^\[[^\]]*\]\s*", "", html.unescape(a.get("title") or n.get("text") or "")),
                         "level": n.get("type")})
    return kids, int(d.get("total") or len(kids))


def _visible_children(slug: str, key: str | None) -> int | None:
    """Stock AtoM: how many records directly below this one a visitor may see."""
    try:
        return tree_page(slug, 0, 1)[1]
    except Exception:
        return None


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
    # Singular, lower case ("record"): the reference service takes either form,
    # OpenRiC Workspace only this one. Results are filtered here as well.
    wanted = ",".join(t.lower() for t in types.split(","))
    hits = _api("GET", "/autocomplete", params={"q": query, "types": wanted, "limit": min(s + n, 50)})
    hits = hits if isinstance(hits, list) else []
    out = []
    for h in hits:
        if h.get("type") in types.split(","):
            # The reference service answers numeric ids; Workspace answers full IRIs.
            if str(h.get("id", "")).startswith("http"):
                h = {**h, "url": h["id"], "id": _ric_ref(h["id"])}
            out.append(h)
    return out[s:s + n]


def _ric_dates(d: dict) -> list[dict]:
    """openricx:hasDateRangeSet: each range as {"date": "1880 - 1950", "type": "creation"}."""
    out = []
    for r in d.get("openricx:hasDateRangeSet") or []:
        if not isinstance(r, dict):
            continue
        text = _ric_label(r.get("rico:expressedDate")) or " - ".join(
            t for t in (_ric_label(r.get("rico:beginningDate")), _ric_label(r.get("rico:endDate"))) if t)
        kind = str((r.get("rico:hasDateType") or {}).get("@id", "")).rstrip("/").rsplit("/", 1)[-1]
        if text:
            out.append({"date": text, **({"type": kind} if kind else {})})
    return out


def _read_openric(slug: str, key: str | None, d: dict | None = None) -> dict:
    d = d if d is not None else _api("GET", f"/records/{slug}", key)
    rid = slug if str(slug).isdigit() else None
    names, places = _ric_labels(d.get("rico:hasCreator")), []
    if rid:  # related agents and places are read by numeric id only
        rel = _api("GET", f"/relations-for/{rid}", key)
        names = sorted(set(names) | {r["target_name"] for r in (rel.get("outgoing") or []) + (rel.get("incoming") or [])
                                     if r.get("target_type") == "Agent" and r.get("target_name")})
        ents = _api("GET", f"/records/{rid}/entities", key, params={"types": "places"})
        places = _ric_labels(ents.get("places") if isinstance(ents, dict) else None)
    holder = d.get("rico:hasOrHadHolder")
    return {
        "found": True, "source": _c.name, "url": d.get("@id"), "slug": slug,
        "title": _ric_label(d.get("rico:title")), "reference_code": _ric_label(d.get("rico:identifier")),
        "level": _ric_label(d.get("rico:type")) or str(d.get("@type", "")).rsplit("#", 1)[-1],
        "dates": _ric_dates(d) or None,
        "extent": _ric_label(d.get("rico:hasExtent")), "scope_and_content": _ric_label(d.get("rico:scopeAndContent")),
        "archival_history": _ric_label(d.get("rico:history")), "names": names,
        "subjects": _ric_labels(d.get("rico:hasOrHadSubject")) or None, "places": places or None, "notes": None,
        "repository": _ric_label(holder), "part_of": [], "children_count": None, "images": [],
    }


def ric_children(slug: str, key: str | None) -> tuple[dict, list[dict]]:
    """The record and the records directly below it, from OpenRiC's hierarchy walk."""
    d = _api("GET", f"/records/{slug}", key)
    rec = _read_openric(slug, key, d)
    if not str(slug).isdigit():
        # Workspace: children are in the record itself (rico:includesOrIncluded).
        items = d.get("rico:includesOrIncluded") or []
        items = items if isinstance(items, list) else [items]
        return rec, [{"slug": _ric_ref(c.get("@id")), "title": _ric_label(c.get("rico:name") or c.get("rico:title")),
                      "level": str(c.get("@type", "")).rsplit(":", 1)[-1], "url": c.get("@id")}
                     for c in items if isinstance(c, dict) and c.get("@id")]
    tree = _api("GET", f"/hierarchy/{slug}", key, params={"include": "children"})
    kids = []
    for c in (tree.get("children") or []) if isinstance(tree, dict) else []:
        cid = c.get("id") or c.get("entity_id")
        if cid:
            kids.append({"slug": str(cid), "title": _ric_label(c.get("title") or c.get("rico:title") or c.get("label")),
                         "level": _ric_label(c.get("level") or c.get("class")), "url": c.get("@id")})
    return rec, kids


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ric_workspace(slug: str) -> bool:
    """OpenRiC Workspace names records by UUID and has files, text and a review
    queue; the reference service names them by id or slug and has none of these."""
    return bool(_UUID.match(str(slug)))


# Local Contexts labels that ask for material not to be reused (Traditional Knowledge:
# Secret/Sacred, Community Use Only, Women Restricted, Men Restricted). Analysing it
# with an assistant is itself such a use, so its files are not handed over.
NO_REUSE_LABELS = {"tk_s", "tk_co", "tk_wr", "tk_mr"}
# PREMIS acts that, when disallowed, mean a file may not be shown or passed on.
NO_SHOW_ACTS = ("render", "disseminate", "use")


def ric_rights(*parts: dict | None) -> dict:
    """OpenRiC Workspace rights (v0.0.59), for a record and one of its
    instantiations (the later part wins for the same act), as the assistant
    should read them: the statements, licences and acts in force, the Local
    Contexts labels, and whether the file may be handed over at all.
    use: "withheld" (a no-reuse label, or render/disseminate/use disallowed),
    "conditions" (anything else stated) or "not_stated"."""
    statements, licences, labels, acts = [], [], [], {}
    for part in parts:
        for rule in (part or {}).get("rules") or []:
            for kind, out in (("statement", statements), ("licence", licences)):
                v = rule.get(kind)
                if v and v.get("code") not in [x["code"] for x in out]:
                    out.append({"code": v.get("code"), "label": v.get("label"), "url": v.get("iri")})
            for act, g in (rule.get("grants") or {}).items():
                acts[act] = {"restriction": (g or {}).get("restriction"), **({"note": g["note"]} if (g or {}).get("note") else {})}
        for lab in (part or {}).get("labels") or []:
            if lab.get("code") not in [x["code"] for x in labels]:
                labels.append({"code": lab.get("code"), "label": lab.get("label"), "url": lab.get("iri")})
    blocked = [x["label"] or x["code"] for x in labels if x["code"] in NO_REUSE_LABELS] + \
              [f"{a} disallowed" for a in NO_SHOW_ACTS if acts.get(a, {}).get("restriction") == "disallow"]
    use = "withheld" if blocked else "conditions" if (statements or licences or labels or acts) else "not_stated"
    out = {"use": use, "statements": statements, "licences": licences, "acts": acts, "labels": labels}
    if blocked:
        out["why"] = "; ".join(blocked)
    return out


def ric_files(slug: str, key: str | None) -> dict:
    """A Workspace record's files and published text, per instantiation (v0.0.49):
    only what a visitor may have, never masters; 404 when the record is not public."""
    return _api("GET", f"/records/{slug}/files", key)


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

