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

"""Settings read at start-up, the call log, and the catalogues this service reads
(home, configured and demo), with the one the current tool call is using (_c).
"""

import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass

from glam_mcp.keycheck import BACKENDS, check as keycheck, detect_backend as _detect_backend


def _setting(name: str, old: str = "", default: str | None = None) -> str:
    """A setting by its name, or by the name it had before (PSIS_*/GLAM_*). With
    no default it is required: an install that forgets it must not quietly use
    another catalogue's address."""
    value = os.environ.get(name) or (os.environ.get(old) if old else None) or default
    if value is None:
        raise SystemExit(f"{name} is required (see README, Install for your own catalogue)")
    return value


PUBLIC_BASE = _setting("CATALOGUE_URL", "PSIS_PUBLIC_BASE").rstrip("/")
API_KEY = _setting("CATALOGUE_API_KEY", "PSIS_API_KEY", "")
BACKEND = os.environ.get("GLAM_BACKEND", "auto")
if BACKEND == "auto":
    BACKEND = _detect_backend(PUBLIC_BASE, API_KEY)
API_BASE = _setting("CATALOGUE_API_URL", "PSIS_API_BASE", PUBLIC_BASE + BACKENDS.get(BACKEND, ("",))[0]).rstrip("/")
SOURCE = _setting("CATALOGUE_NAME", "GLAM_SOURCE_LABEL")
CALL_LOG = os.environ.get("GLAM_CALL_LOG", "")  # file path; empty means stdout
MAX_LIMIT = 25
# Proposal items one signed-in key may queue per hour (names, places, dates,
# relationships, texts), so one session cannot flood the review queues.
# Kept in GLAM_DATA/proposal-cap.db; the catalogue's own per-key hourly and
# daily limits (plugins #223) apply on top.
PROPOSAL_CAP = int(os.environ.get("GLAM_PROPOSAL_CAP", "200"))
ENTITY_TYPES = {"person": "PERSON", "organisation": "ORG", "place": "GPE", "date": "DATE"}

if BACKEND not in BACKENDS:
    raise SystemExit(f"GLAM_BACKEND must be auto or one of {', '.join(BACKENDS)}, not {BACKEND!r}")

# Files this service writes (logs, sign-in store, limits) are for its own user only.
os.umask(0o077)
LOG_DAYS = int(os.environ.get("GLAM_LOG_DAYS", "90"))
log = logging.getLogger("glam-mcp.calls")
log.setLevel(logging.INFO)
log.propagate = False  # only to its own file, never also to the container's output
# httpx logs every request address, search words included; Docker keeps that output.
logging.getLogger("httpx").setLevel(logging.WARNING)
if CALL_LOG:
    # One file a day, kept LOG_DAYS days, then deleted (calls.jsonl.2026-10-26 ...).
    import logging.handlers
    log.addHandler(logging.handlers.TimedRotatingFileHandler(CALL_LOG, when="midnight", backupCount=LOG_DAYS, utc=True))
    os.chmod(CALL_LOG, 0o600)
else:
    log.addHandler(logging.StreamHandler())

# GLAM_AUTH=oauth: the signed-in service (browser assistants sign in, desktop
# clients may send their catalogue key as a bearer). GLAM_AUTH=off: the public,
# read-only service, which never uses a caller key.
AUTH_MODE = os.environ.get("GLAM_AUTH", "off")
MCP_BASE = _setting("MCP_PUBLIC_URL", "GLAM_PUBLIC_BASE", "" if AUTH_MODE == "off" else None).rstrip("/")
HTTP_PATH = os.environ.get("GLAM_HTTP_PATH", "/mcp")
# Where to send people who try to propose on the public, read-only service.
SIGNED_IN_URL = _setting("MCP_SIGNED_IN_URL", default=(MCP_BASE + HTTP_PATH) if AUTH_MODE == "oauth" else "")

# PSIS's nginx drops requests from generic client user agents (bot protection),
# so the service names itself.
USER_AGENT = "AHG-GLAM-MCP/0.2 (+https://www.atom-extensions.com)"
KEY_HEADER = BACKENDS[BACKEND][1]


@dataclass(frozen=True)
class Catalogue:
    name: str
    public_base: str
    api_base: str
    backend: str
    key: str
    key_header: str
    home: bool = False
    demo: bool = False


def _other_catalogues() -> dict[str, Catalogue]:
    """GLAM_OTHER_CATALOGUES: further catalogues to read and search, as a JSON list
    of {"name", "url", "key", "backend"?}. They are read with their own service
    key (give it a user with no groups, so only what a visitor sees comes back)
    and never receive proposals or a caller's key; those stay with the home
    catalogue. One that cannot be reached at start-up is left out, not fatal."""
    out: dict[str, Catalogue] = {}
    for c in json.loads(os.environ.get("GLAM_OTHER_CATALOGUES") or "[]"):
        base = c["url"].rstrip("/")
        try:
            backend = c.get("backend") or _detect_backend(base, c.get("key", ""))
        except SystemExit as e:
            log.warning(json.dumps({"catalogue": c["name"], "skipped": str(e)}))
            continue
        if why := keycheck(base, c.get("key", ""), backend):
            log.warning(json.dumps({"catalogue": c["name"], "skipped": why}))
            continue
        out[c["name"]] = Catalogue(c["name"], base, base + BACKENDS[backend][0], backend, c.get("key", ""), BACKENDS[backend][1])
    return out


HOME = Catalogue(SOURCE, PUBLIC_BASE, API_BASE, BACKEND, API_KEY, KEY_HEADER, home=True)
CATALOGUES = {HOME.name: HOME, **_other_catalogues()}


def _catalogues() -> dict[str, Catalogue]:
    """Home, configured, then demo catalogues added at /catalogues (read each call, so both services see them)."""
    from glam_mcp import catalogues as demo
    out = dict(CATALOGUES)
    for d in demo.load():
        out.setdefault(d["name"], Catalogue(d["name"], d["url"], d["url"] + BACKENDS[d["backend"]][0],
                                            d["backend"], d.get("key", ""), BACKENDS[d["backend"]][1], demo=True))
    return out
_current: ContextVar[Catalogue] = ContextVar("catalogue", default=HOME)


class _Current:
    """The catalogue this tool call reads (the home one unless the caller chose another)."""
    def __getattr__(self, name):
        return getattr(_current.get(), name)


_c = _Current()

