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


def _api(method: str, path: str, key: str | None = None, **kw) -> Any:
    r = _client.request(method, _c.api_base + path, headers={_c.key_header: key or _c.key}, **kw)
    if r.status_code == 404:
        raise NotVisible()
    # 501: the catalogue lacks what this needs (e.g. AHG API without the AI plugin's review queues).
    # 429: the key's hourly or daily limit is used up (atom-ahg-plugins#223).
    if r.status_code in (401, 403, 429, 501):
        try:
            message = r.json().get("message") or "Not authorised"
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
        ok = _client.get(f"{_c.public_base}/{slug}", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 200
    except httpx.HTTPError:
        ok = False
    if len(_visible_cache) > 5000:
        _visible_cache.clear()
    _visible_cache[(_c.public_base, slug)] = (now, ok)
    return ok


def _names_base(d: dict) -> list:
    """Creators and name access points, as names."""
    out = []
    for n in (d.get("creators") or []) + (d.get("name_access_points") or []):
        n = n.get("authorized_form_of_name") if isinstance(n, dict) else n
        if n and n not in out:
            out.append(n)
    return out


def _page_text(slug: str) -> str | None:
    """Stock AtoM only: the record's public page as a visitor gets it, as plain
    text (tags dropped, entities read, spaces collapsed); None when hidden."""
    try:
        r = _client.get(f"{_c.public_base}/{slug}", headers={"Accept": "text/html"}, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", r.text)).split())


def _shown(value: Any, page: str) -> Any:
    """The part of a field a visitor sees on the page, or None. Lists keep only
    the items shown. A text counts as shown when its first 80 characters are."""
    if isinstance(value, list):
        kept = [v for v in value if _shown(v, page) is not None]
        return kept or None
    text = value
    if isinstance(value, dict):
        text = next((value[k] for k in ("content", "date", "authorized_form_of_name", "name", "title") if value.get(k)), "")
    text = " ".join(str(text or "").split())
    return value if text and text[:80] in page else None


def _read_base(slug: str, key: str | None) -> dict:
    """Stock AtoM's REST API returns fields the site may hide from visitors
    (visible-elements settings, archivist's notes). Every field is kept only
    where the public page shows it, for every caller. ponytail: staff with their
    own key see visitor fields too here; full staff reads need the AHG API."""
    page = _page_text(slug)
    _visible_cache[(_c.public_base, slug)] = (time.time(), page is not None)
    if page is None:
        raise NotVisible()
    d = _api("GET", f"/informationobjects/{slug}", key)
    f = lambda name: _shown(d.get(name), page)  # noqa: E731
    return {
        "found": True, "source": _c.name, "url": _url(slug), "slug": slug,
        "title": d.get("title"), "reference_code": f("reference_code"), "level": d.get("level_of_description"),
        "dates": f("dates"), "extent": f("extent_and_medium"), "scope_and_content": f("scope_and_content"),
        "archival_history": f("archival_history"), "names": _shown(_names_base(d), page) or [],
        "subjects": f("subject_access_points"), "places": f("place_access_points"), "notes": f("notes"),
        "repository": f("repository"), "part_of": [], "children_count": None, "images": [],
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

