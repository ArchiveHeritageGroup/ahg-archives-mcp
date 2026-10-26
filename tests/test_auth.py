# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in tests: codes and tokens are single use, stored as hashes, keys
encrypted; revocation and a revoked catalogue key both disconnect."""

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cryptography.fernet import Fernet  # noqa: E402
from mcp.server.auth.provider import AuthorizationCode, TokenError  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402

from glam_mcp import auth  # noqa: E402

KEY = "staff-catalogue-key-123"


class Checker:
    def __init__(self):
        self.good = {KEY}

    def valid(self, key):
        return key in self.good


def run(c):
    return asyncio.run(c)


class SignIn(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "oauth.db")
        self.store = auth.Store(self.path, Fernet.generate_key().decode())
        self.checker = Checker()
        self.p = auth.CatalogueKeyOAuthProvider(self.store, self.checker, "https://mcp.example.org", "Home", "https://cat/api/keys")
        self.client = OAuthClientInformationFull(client_id="c1", redirect_uris=["https://assistant.example/cb"])

    def _code(self):
        code = "code-abc"
        ac = AuthorizationCode(code=code, scopes=[], expires_at=time.time() + 60, client_id="c1", code_challenge="x" * 43,
                               redirect_uri="https://assistant.example/cb", redirect_uri_provided_explicitly=True, subject="key:1")
        self.store.q("INSERT INTO tokens (hash, kind, data, key, expires) VALUES (?, 'code', ?, ?, ?)",
                     (auth._h(code), ac.model_dump_json(), self.store.enc(KEY), ac.expires_at))
        return ac

    def test_tokens_stored_as_hashes_and_key_encrypted(self):
        tok = run(self.p.exchange_authorization_code(self.client, self._code()))
        raw = open(self.path, "rb").read()
        for secret in (tok.access_token, tok.refresh_token, KEY):
            self.assertNotIn(secret.encode(), raw)
        access = run(self.p.load_access_token(tok.access_token))
        self.assertEqual(self.p.catalogue_key_for(access), KEY)

    def test_code_is_single_use(self):
        ac = self._code()
        run(self.p.exchange_authorization_code(self.client, ac))
        with self.assertRaises(TokenError):
            run(self.p.exchange_authorization_code(self.client, ac))

    def test_revoke_ends_both_tokens(self):
        tok = run(self.p.exchange_authorization_code(self.client, self._code()))
        access = run(self.p.load_access_token(tok.access_token))
        run(self.p.revoke_token(access))
        self.assertIsNone(run(self.p.load_access_token(tok.access_token)))
        self.assertIsNone(run(self.p.load_refresh_token(self.client, tok.refresh_token)))

    def test_refresh_rotates_and_revoked_catalogue_key_disconnects(self):
        tok = run(self.p.exchange_authorization_code(self.client, self._code()))
        rt = run(self.p.load_refresh_token(self.client, tok.refresh_token))
        tok2 = run(self.p.exchange_refresh_token(self.client, rt, []))
        self.assertIsNone(run(self.p.load_access_token(tok.access_token)))  # old pair gone
        self.checker.good.clear()  # the key is revoked at the catalogue
        rt2 = run(self.p.load_refresh_token(self.client, tok2.refresh_token))
        with self.assertRaises(TokenError):
            run(self.p.exchange_refresh_token(self.client, rt2, []))

    def test_expired_access_token_refused(self):
        tok = run(self.p.exchange_authorization_code(self.client, self._code()))
        self.store.q("UPDATE tokens SET expires = ? WHERE kind = 'access'", (time.time() - 1,))
        self.assertIsNone(run(self.p.load_access_token(tok.access_token)))

    def test_unknown_bearer_must_be_a_working_catalogue_key(self):
        self.assertIsNone(run(self.p.load_access_token("not-a-key")))
        direct = run(self.p.load_access_token(KEY))
        self.assertEqual(self.p.catalogue_key_for(direct), KEY)

    def test_other_clients_code_refused(self):
        self._code()
        other = OAuthClientInformationFull(client_id="c2", redirect_uris=["https://x.example/cb"])
        self.assertIsNone(run(self.p.load_authorization_code(other, "code-abc")))


if __name__ == "__main__":
    unittest.main()
