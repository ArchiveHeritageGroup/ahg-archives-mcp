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

"""Is a catalogue key visitor-level? Checked before a key serves the public
(a hosted client's service key, a demo catalogue's key), without access to the
catalogue's database:

  - AHG API: a key that may list everyone's proposals is staff, refused;
  - a sample of the records the key reads (newest first, where drafts sit)
    must each open for a visitor on the public site, or the key sees more
    than a visitor does.

ponytail: a sample, not proof; tests/no_leak.py with database access is the
full check. Stock AtoM is read through the visitor check anyway (server.py).

  python -m glam_mcp.keycheck <catalogue-url> <key>     exit 0 when fit
"""

import ipaddress
import socket
import sys
from urllib.parse import urlparse

import httpx

UA = {"User-Agent": "AHG-GLAM-MCP/0.3 (+https://www.atom-extensions.com)", "Accept": "application/json"}
SAMPLE = 40


# Each backend: its API path under the catalogue's address, the header its key goes
# in, a read that proves the API works, and what to call it.
BACKENDS = {
    "atom-ahg": ("/api/v2", "X-API-Key", "/descriptions?limit=1", "AtoM with AHG extensions"),
    "atom-base": ("/api", "REST-API-Key", "/informationobjects?limit=1", "AtoM"),
    "openric": ("/api/ric/v1", "X-API-Key", "/records?limit=1", "OpenRiC"),
}


def detect_backend(public_base: str, key: str) -> str:
    """auto: ask the catalogue which API it has. OpenRiC answers RiC-O JSON-LD
    at /api/ric/v1 (asked first: it also carries a copy of an AHG API); the AHG
    REST API v2 (ahgAPIPlugin) answers JSON at /api/v2, even when it refuses the
    key; stock AtoM's arRestApiPlugin answers at /api. A catalogue that answers
    none cannot be served, so the service stops and is restarted to ask again."""
    for backend in ("openric", "atom-ahg", "atom-base"):
        path, header, probe, _ = BACKENDS[backend]
        try:
            r = httpx.get(public_base + path + probe, headers={**UA, header: key}, timeout=15.0)
            if r.status_code in (200, 401, 403) and "json" in r.headers.get("content-type", ""):
                if backend != "openric" or "openric" in r.text[:2000]:
                    return backend
        except httpx.HTTPError:
            pass
    raise SystemExit(f"GLAM_BACKEND=auto: {public_base} answers neither /api/ric/v1 (OpenRiC), /api/v2 (AHG) nor /api (stock AtoM REST API); "
                     "check CATALOGUE_URL and that the API is enabled, or set GLAM_BACKEND")


def not_public(url: str) -> str | None:
    """Why this address may not be added, or None. Only http(s) to public
    addresses: the service must not be turned on our own network."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "Give the catalogue's full web address, starting https://"
    try:
        addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(u.hostname, u.port or 443)}
    except (socket.gaierror, ValueError):
        return f"{u.hostname} cannot be found."
    if any(not a.is_global for a in addrs):
        return f"{u.hostname} is not a public address."
    return None


def _opens(c: httpx.Client, page: str, hops: int = 3) -> bool:
    """Does this page open for a visitor? A redirect that stays on the same host
    is followed (Heratio sends sector records on to /museum/..., /dam/...); one
    to another host, or to sign-in, does not count as open."""
    for _ in range(hops):
        r = c.get(page, headers={"Accept": "text/html"}, follow_redirects=False)
        if r.status_code == 200:
            return True
        nxt = r.headers.get("location") if r.is_redirect else None
        if not nxt:
            return False
        target = httpx.URL(page).join(nxt)
        if target.host != httpx.URL(page).host or "/login" in target.path:
            return False
        page = str(target)
    return False


def check(url: str, key: str, backend: str) -> str | None:
    """Why the key is not fit to serve the public, or None."""
    url = url.rstrip("/")
    with httpx.Client(timeout=20.0, headers=UA) as c:
        if backend == "atom-ahg":
            h = {"X-API-Key": key}
            if c.get(url + "/api/v2/proposals", params={"mine": 0, "count": 1}, headers=h).status_code == 200:
                return "This key belongs to a catalogue administrator or editor. Give a key for a user with no groups."
            r = c.get(url + "/api/v2/descriptions", params={"limit": SAMPLE}, headers=h)
            if r.status_code != 200:
                return f"The catalogue refused this key ({r.status_code}). Give a key with the Read scope."
            body = r.json()
            slugs = [d["slug"] for d in (body.get("data", body).get("results") or []) if d.get("slug")]
        elif backend == "openric":
            # OpenRiC reads need no key, and its API decides visibility (its record
            # pages send browsers on to a viewer on another host): every record it
            # lists must also open through the API by its slug.
            r = c.get(url + "/api/ric/v1/records", params={"limit": SAMPLE})
            if r.status_code != 200:
                return f"The catalogue's OpenRiC API answered {r.status_code}."
            slugs = [str(i["@id"]).rstrip("/").rsplit("/", 1)[-1] for i in (r.json().get("openric:items") or []) if i.get("@id")]
            hidden = [s for s in slugs if c.get(f"{url}/api/ric/v1/records/{s}").status_code != 200]
            if hidden:
                return f"{len(hidden)} of {len(slugs)} sampled records do not open through the API (for example {hidden[0]})."
            return None
        else:
            r = c.get(url + "/api/informationobjects", params={"limit": SAMPLE}, headers={"REST-API-Key": key})
            if r.status_code != 200:
                return f"The catalogue refused this key ({r.status_code}). Give the REST API key of a user with no groups."
            return None  # stock AtoM: every record is checked against the public page as it is read
        hidden = [s for s in slugs if not _opens(c, f"{url}/{s}")]
        if hidden:
            return (f"This key reads {len(hidden)} of {len(slugs)} sampled records a visitor cannot open (for example {hidden[0]}). "
                    "Give a key for a user with no groups.")
    return None


if __name__ == "__main__":
    u, k = sys.argv[1], sys.argv[2]
    why = check(u, k, detect_backend(u.rstrip("/"), k))
    print(why or "ok: the key reads only what a visitor sees (sampled)")
    sys.exit(1 if why else 0)
