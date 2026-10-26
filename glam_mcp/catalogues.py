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

"""Demo catalogues (/catalogues, staff only): add a client's catalogue for a
show and tell, so their own records can be searched and read from an agent
(list_catalogues, then catalogue="<name>" on any read tool).

Read only, with a key the client gives for a user with no groups: a demo catalogue never receives proposals or a
caller's key. Each one lapses after GLAM_DEMO_DAYS (7). Kept in
<GLAM_DATA>/demo-catalogues.json, shared by both services. ponytail: keys are
stored as given (file mode 600); give only a key of a user with no groups.
"""

import html
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlparse

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse

from glam_mcp.usage import _page, _table

DEMO_DAYS = int(os.environ.get("GLAM_DEMO_DAYS", "7"))
PATH = os.path.join(os.environ.get("GLAM_DATA", "/data"), "demo-catalogues.json")


def load() -> list[dict]:
    """Demo catalogues still within their days."""
    try:
        with open(PATH, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("added", 0) > time.time() - DEMO_DAYS * 86400]


def _save(rows: list[dict]) -> None:
    tmp = PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(rows, f)
    os.replace(tmp, PATH)


def _outside_address(url: str) -> str | None:
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


def make_catalogues_route(api_base: str, key_header: str, user_agent: str, source: str, reserved, detect):
    """reserved(): names already taken (home and configured catalogues).
    detect(url, key): "atom-ahg" or "atom-base", raising SystemExit when neither answers."""

    def _list(message: str = "", error: bool = False) -> str:
        rows = [[r["name"], r["url"], "AtoM with AHG extensions" if r["backend"] == "atom-ahg" else "AtoM",
                 time.strftime("%d %b %Y", time.localtime(r["added"] + DEMO_DAYS * 86400))] for r in load()]
        note = f'<p class="{"err" if error else ""}">{html.escape(message)}</p>' if message else ""
        return f"""<p>Add a client's catalogue for a show and tell. Agents connected to this service then see it in
list_catalogues and can search and read it (catalogue="name"), as a visitor would. Nothing is ever proposed to it.
Each one lapses after {DEMO_DAYS} days. For administrators and editors of {html.escape(source)}.</p>{note}
<h2>Demo catalogues</h2>{_table(["Name", "Address", "Kind", "Lapses"], rows, numeric_from=99)}
<h2>Add or remove</h2>
<form method="post"><label>Your catalogue API key ({html.escape(source)})<br><input type="password" name="staff_key" autocomplete="off" required></label>
<p><label>Name shown to the agent<br><input name="name" maxlength="60" placeholder="e.g. Example Municipal Archives"></label></p>
<p><label>Catalogue web address<br><input name="url" placeholder="https://catalogue.example.org"></label></p>
<p><label>Its read-only API key (from its API keys page, or AtoM's REST API key; make it for a user with no groups, so only what a visitor sees comes back)<br><input type="password" name="key" autocomplete="off"></label></p>
<p><button type="submit" name="do" value="add">Add</button> <button type="submit" name="do" value="remove">Remove by name</button></p></form>"""

    async def catalogues(request: Request) -> HTMLResponse:
        if request.method == "GET":
            return _page("Demo catalogues", _list())
        form = await request.form()
        staff = str(form.get("staff_key", "")).strip()
        try:
            r = httpx.get(api_base + "/proposals", params={"mine": 0, "count": 1},
                          headers={key_header: staff, "User-Agent": user_agent, "Accept": "application/json"}, timeout=20)
        except httpx.HTTPError:
            return _page("Demo catalogues", _list("The catalogue could not be reached. Try again later.", True), 503)
        if not staff or r.status_code != 200:
            return _page("Demo catalogues", _list("That key may not manage demo catalogues: it needs a catalogue administrator or editor.", True), 403)

        name = " ".join(str(form.get("name", "")).split())[:60]
        rows = load()
        if form.get("do") == "remove":
            kept = [x for x in rows if x["name"] != name]
            _save(kept)
            return _page("Demo catalogues", _list(f"Removed {name}." if len(kept) < len(rows) else f"No demo catalogue called {name}."))

        url = str(form.get("url", "")).strip().rstrip("/")
        key = str(form.get("key", "")).strip()
        if not name or name in reserved() or name.lower() == "all":
            return _page("Demo catalogues", _list("Give it a name not already used.", True), 400)
        if not key:
            return _page("Demo catalogues", _list("Give the catalogue's read-only API key: both kinds of AtoM refuse reads without one.", True), 400)
        if why := _outside_address(url):
            return _page("Demo catalogues", _list(why, True), 400)
        try:
            backend = detect(url, key)
        except SystemExit:
            return _page("Demo catalogues", _list(f"{url} answers neither the AHG API nor AtoM's REST API. Check the address, and that the API is switched on.", True), 400)
        rows = [x for x in rows if x["name"] != name] + [{"name": name, "url": url, "key": key, "backend": backend, "added": time.time()}]
        _save(rows)
        return _page("Demo catalogues", _list(f"Added {name}. In the agent: list_catalogues, then use catalogue=\"{name}\"."))

    return catalogues
