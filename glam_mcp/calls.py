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

"""Per-call policy and telemetry: whose key a call uses, the hourly proposal
limit, and one log line per tool call (free text hashed).
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time

from mcp.server.fastmcp import Context

from glam_mcp.catalogue import NotVisible, Refused
from glam_mcp.settings import _c, _catalogues, _current, AUTH_MODE, HOME, log, PROPOSAL_CAP, SIGNED_IN_URL


# The sign-in provider (server.py sets it on the signed-in service); None on the public one.
_provider = None


# Free text a caller typed (search words, names, proposed text) is not kept:
# only its length and a short hash, enough to count repeats.
_FREE_TEXT = {"query", "name", "text", "value", "evidence", "notes"}


def _safe_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and (k in _FREE_TEXT or len(v) > 200):
            out[k] = {"chars": len(v), "hash": hashlib.sha256(v.encode()).hexdigest()[:8]} if v else ""
        else:
            out[k] = v
    return out


class NeedsCallerKey(Exception):
    """A write was asked for without the archivist's own key."""


def _caller_key(ctx: Context | None) -> str | None:
    """The catalogue key of the signed-in user, or None on the public service.

    The bearer has already been checked by the auth middleware: it is either one
    of our OAuth tokens (mapped to the key the user gave at sign-in) or a
    catalogue key sent directly by a desktop client.
    """
    if _provider is None or not _c.home:
        return None
    from mcp.server.auth.middleware.auth_context import get_access_token
    return _provider.catalogue_key_for(get_access_token())


def _read_key(ctx: Context | None, catalogue: str) -> str | None:
    """The caller's key for reads in the home catalogue; other catalogues are read with their own service key."""
    return _caller_key(ctx) if catalogue in ("", HOME.name) else None


def _unknown_catalogue(name: str) -> dict:
    return {"found": False, "message": f"No catalogue called {name!r} here. Catalogues: " + ", ".join(_catalogues()) + ".", "source": HOME.name}


def _logged(tool: str, args: dict, ctx: Context | None, fn):
    start = time.time()
    outcome, count = "ok", None
    name = args.get("catalogue") or HOME.name
    cats = _catalogues()
    if name not in cats:
        return _unknown_catalogue(name)
    token = _current.set(cats[name])
    try:
        result = fn()
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            count = len(result["results"])
        return result
    except NotVisible:
        outcome = "not_found"
        return {"found": False, "message": "No visible record with that reference.", "source": _c.name}
    except NeedsCallerKey:
        outcome = "needs_key"
        return {"found": False, "message": "Proposing changes needs you to be signed in. Connect to " + (SIGNED_IN_URL or "this catalogue's signed-in MCP address") + " and sign in with your catalogue API key; the public read-only address cannot propose.", "source": _c.name}
    except Refused as e:
        outcome = "refused"
        return {"found": False, "message": e.message, "source": _c.name}
    except Exception as e:  # never show the caller internals
        outcome = "error:" + type(e).__name__
        return {"found": False, "message": "The catalogue could not be reached. Try again later.", "source": _c.name}
    finally:
        log.info(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "args": _safe_args(args), "caller": "own-key" if (ck := _caller_key(ctx)) else "public",
            # Tells one person's calls from another's for usage figures; reveals nothing of the key.
            **({"key": hashlib.sha256(ck.encode()).hexdigest()[:8]} if ck else {}),
            **({} if _c.home else {"catalogue": _c.name}),
            "outcome": outcome, "results": count, "ms": int((time.time() - start) * 1000),
        }))
        _current.reset(token)


def _needs_key(ctx: Context | None) -> str:
    key = _caller_key(ctx)
    if not key:
        raise NeedsCallerKey()
    return key


# Kept on disk (GLAM_DATA), so a restart does not reset anyone's hour.
_cap_lock = threading.Lock()
try:
    _cap_db = sqlite3.connect(os.path.join(os.environ.get("GLAM_DATA", "/data"), "proposal-cap.db"), check_same_thread=False)
    _cap_db.execute("SELECT 1")
except sqlite3.OperationalError:
    # The signed-in service takes proposals: its limit must survive a restart.
    if AUTH_MODE == "oauth":
        raise SystemExit("GLAM_DATA must be a writable folder on the signed-in service (sign-in store and proposal limit)")
    # The public service takes no proposals (a bare test run, or no data folder): in memory is enough.
    logging.getLogger("glam-mcp").warning("GLAM_DATA not writable: the hourly proposal limit is kept in memory")
    _cap_db = sqlite3.connect(":memory:", check_same_thread=False)
_cap_db.execute("CREATE TABLE IF NOT EXISTS spent (key TEXT NOT NULL, ts REAL NOT NULL)")
_cap_db.execute("CREATE INDEX IF NOT EXISTS spent_key_ts ON spent (key, ts)")


def _budget(key: str, n: int = 0) -> int:
    """Spend n proposal items for this key and return what is left this hour.
    Refuses, spending nothing, when n would go over the cap."""
    k = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    with _cap_lock, _cap_db:
        _cap_db.execute("DELETE FROM spent WHERE ts < ?", (now - 3600,))
        used, first = _cap_db.execute("SELECT COUNT(*), MIN(ts) FROM spent WHERE key = ?", (k,)).fetchone()
        if n and used + n > PROPOSAL_CAP:
            wait = int(first + 3600 - now) // 60 + 1 if first else 60
            raise Refused(f"At most {PROPOSAL_CAP} proposals an hour per person; {PROPOSAL_CAP - used} left. "
                          f"Try again in about {wait} minutes, or propose fewer at once.")
        _cap_db.executemany("INSERT INTO spent (key, ts) VALUES (?, ?)", [(k, now)] * n)
        return PROPOSAL_CAP - used - n

