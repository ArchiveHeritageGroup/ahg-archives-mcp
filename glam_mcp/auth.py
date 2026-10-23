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

"""OAuth sign-in for the GLAM MCP.

Browser-based assistants connect through OAuth. Our sign-in page asks the user
for their own catalogue API key (created at /api/keys on the catalogue), checks
it against the catalogue, and gives the assistant a token of its own. The key
never reaches the assistant; it is kept here encrypted, and every call the
assistant makes runs with that key, so the catalogue's own permissions apply.

Desktop clients that send the catalogue key directly as a bearer token keep
working: a bearer that is not one of our tokens is checked against the
catalogue as a key.

Stored: client registrations, and tokens as SHA-256 hashes only. Keys are
encrypted with GLAM_SECRET (a Fernet key). ponytail: SQLite in the data volume;
move to the shared database if several service instances ever run.
"""

import hashlib
import html
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 600
KEY_CHECK_TTL = 300
DIRECT_KEY_CLIENT = "catalogue-key"  # client_id for bearers that are catalogue keys


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: str, secret: str):
        self.fernet = Fernet(secret.encode())
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, info TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, client_id TEXT, params TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS tokens (
                hash TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                key TEXT NOT NULL, pair TEXT, expires REAL);
        """)
        self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def enc(self, key: str) -> str:
        return self.fernet.encrypt(key.encode()).decode()

    def dec(self, blob: str) -> str | None:
        try:
            return self.fernet.decrypt(blob.encode()).decode()
        except InvalidToken:
            return None

    def purge(self):
        now = time.time()
        self.q("DELETE FROM pending WHERE expires < ?", (now,))
        self.q("DELETE FROM tokens WHERE expires IS NOT NULL AND expires < ?", (now,))


class CatalogueKeyChecker:
    """Is this a working catalogue API key? Cached briefly so each call does not re-ask."""

    def __init__(self, api_base: str, key_header: str, user_agent: str, probe_path: str):
        self.api_base, self.key_header, self.user_agent, self.probe = api_base, key_header, user_agent, probe_path
        self.cache: dict[str, float] = {}

    def valid(self, key: str) -> bool:
        if not key or len(key) > 200:
            return False
        h = _h(key)
        if self.cache.get(h, 0) > time.time():
            return True
        try:
            r = httpx.get(self.api_base + self.probe, timeout=10,
                          headers={self.key_header: key, "Accept": "application/json", "User-Agent": self.user_agent})
            ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            self.cache[h] = time.time() + KEY_CHECK_TTL
        return ok


class CatalogueKeyOAuthProvider:
    def __init__(self, store: Store, checker: CatalogueKeyChecker, public_base: str, catalogue_name: str, keys_url: str):
        self.store, self.checker = store, checker
        self.public_base, self.catalogue_name, self.keys_url = public_base.rstrip("/"), catalogue_name, keys_url

    # ── clients (dynamic registration) ───────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.store.q("SELECT info FROM clients WHERE id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.q("INSERT OR REPLACE INTO clients (id, info) VALUES (?, ?)",
                     (client_info.client_id, client_info.model_dump_json()))

    # ── authorization: send the user to our sign-in page ─────────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.store.purge()
        req = secrets.token_urlsafe(24)
        self.store.q("INSERT INTO pending (id, client_id, params, expires) VALUES (?, ?, ?, ?)",
                     (req, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL))
        return f"{self.public_base}/login?req={req}"

    def _page(self, req: str, redirect_uri: str, error: str = "") -> HTMLResponse:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in - AHG Archives</title>
<style>body{{font-family:Arial,sans-serif;background:#F2EDE6;color:#262626;margin:0}}
main{{max-width:30rem;margin:3rem auto;background:#fff;padding:2rem;border-top:6px solid #10373E}}
h1{{color:#10373E;font-size:1.4rem}}label{{display:block;font-weight:bold;margin:1rem 0 .3rem}}
input{{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}}
button{{margin-top:1rem;background:#10373E;color:#fff;border:0;padding:.7rem 1.4rem;font-size:1rem;cursor:pointer}}
.err{{color:#653332;font-weight:bold}}small{{color:#555}}</style></head><body><main>
<h1>Connect your assistant to {html.escape(self.catalogue_name)}</h1>
<p>Paste your own catalogue API key. Your assistant will be able to read what you may read and
suggest changes for review under your name. It never sees the key itself.</p>
{err}<form method="post" action="/login">
<input type="hidden" name="req" value="{html.escape(req)}">
<label for="key">Your API key</label>
<input id="key" name="key" type="password" autocomplete="off" required>
<button type="submit">Connect</button></form>
<p><small>No key yet? Sign in to the catalogue and create one at
<a href="{html.escape(self.keys_url)}">{html.escape(self.keys_url)}</a> with the Read and
Create and update scopes. You can revoke it there at any time, which disconnects the assistant.</small></p>
</main></body></html>"""
        # form-action also governs where the submission redirects, so the assistant's
        # own callback origin must be allowed, or the browser silently stops after a
        # successful sign-in (found with the first real browser connection).
        u = urlparse(redirect_uri)
        target = f"{u.scheme}://{u.netloc}" if u.scheme in ("https", "http") and u.netloc else ""
        csp = f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {target}".strip()
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": csp})

    async def login_get(self, request: Request) -> Response:
        req = request.query_params.get("req", "")
        rows = self.store.q("SELECT params FROM pending WHERE id = ? AND expires >= ?", (req, time.time()))
        if not rows:
            return HTMLResponse("<p>This sign-in link has expired. Start the connection again from your assistant.</p>", status_code=400)
        return self._page(req, str(AuthorizationParams.model_validate_json(rows[0][0]).redirect_uri))

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        req, key = str(form.get("req", "")), str(form.get("key", "")).strip()
        rows = self.store.q("SELECT client_id, params FROM pending WHERE id = ? AND expires >= ?", (req, time.time()))
        if not rows:
            return HTMLResponse("<p>This sign-in link has expired. Start the connection again from your assistant.</p>", status_code=400)
        if not self.checker.valid(key):
            return self._page(req, str(AuthorizationParams.model_validate_json(rows[0][1]).redirect_uri),
                              "That key was not accepted by the catalogue. Check it, or create a new one.")
        self.store.q("DELETE FROM pending WHERE id = ?", (req,))  # single use
        client_id, params = rows[0][0], AuthorizationParams.model_validate_json(rows[0][1])
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL,
                               client_id=client_id, code_challenge=params.code_challenge,
                               redirect_uri=params.redirect_uri,
                               redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                               resource=params.resource, subject="key:" + _h(key)[:12])
        self.store.q("INSERT INTO tokens (hash, kind, data, key, expires) VALUES (?, 'code', ?, ?, ?)",
                     (_h(code), ac.model_dump_json(), self.store.enc(key), ac.expires_at))
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302)

    # ── codes and tokens ──────────────────────────────────────────────────
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        rows = self.store.q("SELECT data FROM tokens WHERE hash = ? AND kind = 'code'", (_h(authorization_code),))
        if not rows:
            return None
        ac = AuthorizationCode.model_validate_json(rows[0][0])
        return ac if ac.client_id == client.client_id and ac.expires_at >= time.time() else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None, enc_key: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        at = AccessToken(token="", client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL, resource=resource, subject=subject)
        rt = RefreshToken(token="", client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL, resource=resource, subject=subject)
        self.store.q("INSERT INTO tokens (hash, kind, data, key, pair, expires) VALUES (?, 'access', ?, ?, ?, ?)",
                     (_h(access), at.model_dump_json(), enc_key, _h(refresh), at.expires_at))
        self.store.q("INSERT INTO tokens (hash, kind, data, key, pair, expires) VALUES (?, 'refresh', ?, ?, ?, ?)",
                     (_h(refresh), rt.model_dump_json(), enc_key, _h(access), rt.expires_at))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes) if scopes else None)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        rows = self.store.q("SELECT key FROM tokens WHERE hash = ? AND kind = 'code'", (_h(authorization_code.code),))
        self.store.q("DELETE FROM tokens WHERE hash = ?", (_h(authorization_code.code),))  # single use
        if not rows:
            from mcp.server.auth.provider import TokenError
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource, authorization_code.subject, rows[0][0])

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        rows = self.store.q("SELECT data FROM tokens WHERE hash = ? AND kind = 'refresh'", (_h(refresh_token),))
        if not rows:
            return None
        rt = RefreshToken.model_validate_json(rows[0][0])
        if rt.client_id != client.client_id or (rt.expires_at and rt.expires_at < time.time()):
            return None
        return rt.model_copy(update={"token": refresh_token})

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        rows = self.store.q("SELECT key, pair FROM tokens WHERE hash = ? AND kind = 'refresh'", (_h(refresh_token.token),))
        if not rows:
            from mcp.server.auth.provider import TokenError
            raise TokenError("invalid_grant", "refresh token not found")
        enc_key, pair = rows[0]
        key = self.store.dec(enc_key)
        self.store.q("DELETE FROM tokens WHERE hash IN (?, ?)", (_h(refresh_token.token), pair))  # rotate
        if not key or not self.checker.valid(key):  # key revoked at the catalogue: disconnect
            from mcp.server.auth.provider import TokenError
            raise TokenError("invalid_grant", "the catalogue key behind this connection is no longer valid")
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, refresh_token.subject, enc_key)

    async def load_access_token(self, token: str) -> AccessToken | None:
        rows = self.store.q("SELECT data, expires FROM tokens WHERE hash = ? AND kind = 'access'", (_h(token),))
        if rows:
            if rows[0][1] and rows[0][1] < time.time():
                return None
            return AccessToken.model_validate_json(rows[0][0]).model_copy(update={"token": token})
        # Not one of ours: a desktop client sending its catalogue key directly.
        if self.checker.valid(token):
            return AccessToken(token=token, client_id=DIRECT_KEY_CLIENT, scopes=[], subject="key:" + _h(token)[:12])
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        rows = self.store.q("SELECT pair FROM tokens WHERE hash = ?", (_h(token.token),))
        pair = rows[0][0] if rows else None
        self.store.q("DELETE FROM tokens WHERE hash IN (?, ?)", (_h(token.token), pair or ""))

    # ── for the tools: the catalogue key behind the current bearer ───────
    def catalogue_key_for(self, access: AccessToken | None) -> str | None:
        if access is None:
            return None
        if access.client_id == DIRECT_KEY_CLIENT:
            return access.token
        rows = self.store.q("SELECT key FROM tokens WHERE hash = ? AND kind = 'access'", (_h(access.token),))
        return self.store.dec(rows[0][0]) if rows else None
