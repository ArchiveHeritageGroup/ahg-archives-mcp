# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool, error, catalogue and safety tests against a fake catalogue (no network).

Run in the image, where the service's own libraries are:
    docker run --rm -v "$PWD/tests:/app/tests:ro" ahg/glam-mcp:phase2 python -m unittest discover -s tests -v
"""

import functools
import json
import os
import sys
import tempfile
import unittest

import httpx

TMP = tempfile.mkdtemp()
os.environ.update({
    "CATALOGUE_URL": "https://cat.example.org", "CATALOGUE_NAME": "Home", "CATALOGUE_API_KEY": "svc-key",
    "GLAM_BACKEND": "atom-ahg", "GLAM_AUTH": "off", "GLAM_DATA": TMP, "GLAM_CALL_LOG": os.path.join(TMP, "calls.jsonl"),
    "GLAM_PROPOSAL_CAP": "5",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from glam_mcp import keycheck, server  # noqa: E402

HOME = "https://cat.example.org"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


class FakeCatalogue:
    """Answers by (method, path); records every request it is sent."""

    def __init__(self):
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        hit = self.routes.get((request.method, request.url.host + request.url.path))
        return hit if hit is not None else httpx.Response(404, json={"message": "Not found"})

    def json(self, method, host_path, body, status=200):
        self.routes[(method, host_path)] = httpx.Response(status, json=body)


class Base(unittest.TestCase):
    def setUp(self):
        self.cat = FakeCatalogue()
        server._client = httpx.Client(transport=httpx.MockTransport(self.cat), headers={"Accept": "application/json"})
        self._real_client = keycheck.httpx.Client
        keycheck.httpx.Client = functools.partial(httpx.Client, transport=httpx.MockTransport(self.cat))
        server.CATALOGUES = {"Home": server.HOME}

    def tearDown(self):
        keycheck.httpx.Client = self._real_client


class Reads(Base):
    def test_search_and_citable_urls(self):
        self.cat.json("POST", "cat.example.org/api/v2/search", {"data": {"total": 1, "results": [{"slug": "a-letter", "title": "A letter"}]}})
        r = server.search_records("letter")
        self.assertEqual(r["results"][0]["url"], f"{HOME}/a-letter")
        self.assertEqual(r["source"], "Home")

    def test_not_visible_and_missing_answer_the_same(self):
        r = server.get_record("hidden-record")
        self.assertFalse(r["found"])
        self.assertEqual(r["message"], "No visible record with that reference.")

    def test_refusals_pass_the_catalogues_message(self):
        for status, msg in ((401, "Not signed in"), (403, "Read scope required"), (429, "Daily limit used up"), (501, "Not supported here")):
            self.cat.json("GET", "cat.example.org/api/v2/descriptions/x", {"message": msg}, status)
            r = server.get_record("x")
            self.assertEqual((r["found"], r["message"]), (False, msg), status)

    def test_server_errors_show_no_internals(self):
        self.cat.routes[("GET", "cat.example.org/api/v2/descriptions/x")] = httpx.Response(500, text="Traceback: secret path /var/www")
        r = server.get_record("x")
        self.assertEqual(r["message"], "The catalogue could not be reached. Try again later.")
        self.assertNotIn("secret", json.dumps(r))


class Catalogues(Base):
    def setUp(self):
        super().setUp()
        other = server.Catalogue("Other", "https://other.example.net", "https://other.example.net/api/v2", "atom-ahg", "other-svc", "X-API-Key")
        server.CATALOGUES = {"Home": server.HOME, "Other": other}

    def test_unknown_catalogue(self):
        r = server.get_record("x", catalogue="Nope")
        self.assertIn("No catalogue called 'Nope'", r["message"])

    def test_all_searches_each_catalogue(self):
        for host in ("cat.example.org", "other.example.net"):
            self.cat.json("POST", f"{host}/api/v2/search", {"data": {"total": 1, "results": [{"slug": "s", "title": host}]}})
        r = server.search_records("x", catalogue="all")
        self.assertEqual([c["source"] for c in r["catalogues"]], ["Home", "Other"])
        self.assertEqual(r["catalogues"][1]["results"][0]["url"], "https://other.example.net/s")

    def test_other_catalogue_gets_its_own_key_never_the_callers(self):
        server._caller_key, real = (lambda ctx: "callers-own-key"), server._caller_key
        try:
            self.cat.json("GET", "other.example.net/api/v2/descriptions/s", {"title": "T"})
            server.get_record("s", catalogue="Other")
        finally:
            server._caller_key = real
        sent = [r.headers.get("X-API-Key") for r in self.cat.seen if r.url.host == "other.example.net"]
        self.assertEqual(sent, ["other-svc"])


class Images(Base):
    def test_file_links_must_be_plain_paths(self):
        for bad in ("https://evil.example/x.jpg", "//evil.example/x.jpg", "@evil.example/x.jpg", "/x@evil/y", "x.jpg", "/a\\b", None):
            self.assertIsNone(server._file_url(bad), bad)
        self.assertEqual(server._file_url("/uploads/r/x.jpg"), f"{HOME}/uploads/r/x.jpg")

    def _img(self, status=200, ctype="image/png", body=PNG, headers=None):
        self.cat.routes[("GET", "cat.example.org/uploads/x.png")] = httpx.Response(status, content=body, headers={"content-type": ctype, **(headers or {})})

    def test_other_host_is_not_fetched(self):
        with self.assertRaises(server.ImageRefused) as e:
            server._fetch_image("https://evil.example/x.png")
        self.assertEqual(e.exception.outcome, "off_site")
        self.assertEqual(self.cat.seen, [])

    def test_image_types_only(self):
        self._img(ctype="text/html", body=b"<html>")
        with self.assertRaises(server.ImageRefused) as e:
            server._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(e.exception.outcome, "not_image")

    def test_redirects_are_not_followed(self):
        self._img(status=302, headers={"location": "http://169.254.169.254/"})
        with self.assertRaises(server.ImageRefused):
            server._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(len(self.cat.seen), 1)

    def test_stops_at_the_size_limit(self):
        self._img(body=b"0" * (server.MAX_IMAGE_BYTES + 1))
        with self.assertRaises(server.ImageRefused) as e:
            server._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(e.exception.outcome, "too_large")

    def test_good_image(self):
        self._img()
        data, fmt = server._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual((data, fmt), (PNG, "png"))


class StockFields(unittest.TestCase):
    def test_only_fields_the_page_shows(self):
        page = "Published letter Letter from Jan Pretorius to his sister Anna Pretoria Jan Pretorius 1902-05-03"
        self.assertEqual(server._shown("Letter from Jan Pretorius to his sister Anna", page), "Letter from Jan Pretorius to his sister Anna")
        self.assertIsNone(server._shown("Archivist's note: donor asked for this to stay private", page))
        self.assertEqual(server._shown(["Pretoria", "Secret place"], page), ["Pretoria"])
        self.assertEqual(server._shown([{"date": "1902-05-03", "type": "Creation"}], page), [{"date": "1902-05-03", "type": "Creation"}])
        self.assertIsNone(server._shown([], page))


class Proposals(Base):
    def test_public_service_cannot_propose(self):
        r = server.propose_description_change("x", "scope_and_content", "New text")
        self.assertIn("needs you to be signed in", r["message"])
        self.assertEqual([q for q in self.cat.seen if q.method == "POST"], [])

    def test_hourly_cap_is_kept_on_disk(self):
        self.assertEqual(server._budget("k1", 3), 2)
        # A second connection to the same file, as after a restart.
        import sqlite3
        db = sqlite3.connect(os.path.join(TMP, "proposal-cap.db"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM spent").fetchone()[0], 3)
        with self.assertRaises(server.Refused):
            server._budget("k1", 3)
        self.assertEqual(server._budget("k1"), 2)  # a refusal spends nothing
        self.assertEqual(server._budget("k2", 5), 0)  # each key has its own hour


class KeyCheck(Base):
    def test_staff_key_refused(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"data": {"results": []}})
        self.assertIn("administrator or editor", keycheck.check(HOME, "staff", "atom-ahg"))

    def test_key_that_reads_drafts_refused(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"message": "Forbidden"}, 403)
        self.cat.json("GET", "cat.example.org/api/v2/descriptions", {"data": {"results": [{"slug": "public-one"}, {"slug": "draft-one"}]}})
        self.cat.routes[("GET", "cat.example.org/public-one")] = httpx.Response(200, text="<html>")
        self.assertIn("1 of 2 sampled records", keycheck.check(HOME, "k", "atom-ahg"))

    def test_visitor_key_passes(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"message": "Forbidden"}, 403)
        self.cat.json("GET", "cat.example.org/api/v2/descriptions", {"data": {"results": [{"slug": "public-one"}]}})
        self.cat.routes[("GET", "cat.example.org/public-one")] = httpx.Response(200, text="<html>")
        self.assertIsNone(keycheck.check(HOME, "k", "atom-ahg"))

    def test_private_addresses_refused(self):
        for url in ("http://127.0.0.1:8097", "http://10.0.0.5", "file:///etc/passwd", "localhost"):
            self.assertIsNotNone(keycheck.not_public(url), url)


class Logs(Base):
    def test_free_text_is_not_kept(self):
        self.cat.json("POST", "cat.example.org/api/v2/search", {"data": {"total": 0, "results": []}})
        server.search_records("my grandmother Anna Pretorius")
        with open(os.environ["GLAM_CALL_LOG"]) as f:
            last = json.loads(f.readlines()[-1])
        self.assertNotIn("Pretorius", json.dumps(last))
        self.assertEqual(last["args"]["query"]["chars"], len("my grandmother Anna Pretorius"))
        self.assertEqual(oct(os.stat(os.environ["GLAM_CALL_LOG"]).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
