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
from glam_mcp import calls, catalogue, keycheck, server, settings  # noqa: E402

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
        catalogue._client = httpx.Client(transport=httpx.MockTransport(self.cat), headers={"Accept": "application/json"})
        self._real_client = keycheck.httpx.Client
        keycheck.httpx.Client = functools.partial(httpx.Client, transport=httpx.MockTransport(self.cat))
        settings.CATALOGUES = {"Home": settings.HOME}

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
        other = settings.Catalogue("Other", "https://other.example.net", "https://other.example.net/api/v2", "atom-ahg", "other-svc", "X-API-Key")
        settings.CATALOGUES = {"Home": settings.HOME, "Other": other}

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
        calls._caller_key, real = (lambda ctx: "callers-own-key"), calls._caller_key
        try:
            self.cat.json("GET", "other.example.net/api/v2/descriptions/s", {"title": "T"})
            server.get_record("s", catalogue="Other")
        finally:
            calls._caller_key = real
        sent = [r.headers.get("X-API-Key") for r in self.cat.seen if r.url.host == "other.example.net"]
        self.assertEqual(sent, ["other-svc"])


class Images(Base):
    def test_file_links_must_be_plain_paths(self):
        for bad in ("https://evil.example/x.jpg", "//evil.example/x.jpg", "@evil.example/x.jpg", "/x@evil/y", "x.jpg", "/a\\b", None):
            self.assertIsNone(catalogue._file_url(bad), bad)
        self.assertEqual(catalogue._file_url("/uploads/r/x.jpg"), f"{HOME}/uploads/r/x.jpg")

    def _img(self, status=200, ctype="image/png", body=PNG, headers=None):
        self.cat.routes[("GET", "cat.example.org/uploads/x.png")] = httpx.Response(status, content=body, headers={"content-type": ctype, **(headers or {})})

    def test_other_host_is_not_fetched(self):
        with self.assertRaises(catalogue.ImageRefused) as e:
            catalogue._fetch_image("https://evil.example/x.png")
        self.assertEqual(e.exception.outcome, "off_site")
        self.assertEqual(self.cat.seen, [])

    def test_image_types_only(self):
        self._img(ctype="text/html", body=b"<html>")
        with self.assertRaises(catalogue.ImageRefused) as e:
            catalogue._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(e.exception.outcome, "not_image")

    def test_redirects_are_not_followed(self):
        self._img(status=302, headers={"location": "http://169.254.169.254/"})
        with self.assertRaises(catalogue.ImageRefused):
            catalogue._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(len(self.cat.seen), 1)

    def test_stops_at_the_size_limit(self):
        self._img(body=b"0" * (catalogue.MAX_IMAGE_BYTES + 1))
        with self.assertRaises(catalogue.ImageRefused) as e:
            catalogue._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual(e.exception.outcome, "too_large")

    def test_good_image(self):
        self._img()
        data, fmt = catalogue._fetch_image(f"{HOME}/uploads/x.png")
        self.assertEqual((data, fmt), (PNG, "png"))


def _field(label, value_html):
    """One field as AtoM 2.10 renders it on a public record page."""
    return (f'<div class="field text-break row g-0"><h3 class="h6 lh-base m-0 text-muted col-3 border-end text-end p-2">'
            f'{label}</h3><div class="col-9 p-2">{value_html}</div></div>')


STOCK_PAGE = "<html><body><h1>Letter of 1902 about Pretoria</h1>" + "".join([
    _field("Title", "Letter of 1902 about Pretoria"),
    _field("Date(s)", '<ul><li> 1902-05-03 (Creation) </li></ul>'),
    _field("Scope and content", "<p>Letter from Jan Pretorius to his sister Anna, Pretoria, 3 May 1902.</p>"),
    _field("Archival history", ""),  # hidden from visitors by the visible-elements settings: shown empty
    _field("Place access points", '<ul><li><a href="/pretoria">Pretoria</a></li></ul>'),
    _field("Name access points", '<ul><li><a href="/jan">Jan Pretorius</a></li></ul>'),
]) + "<h2>Explore</h2><p>Donor restriction applies to the Engelbrecht papers</p></body></html>"

STOCK_API = {
    "title": "Letter of 1902 about Pretoria", "reference_code": "TEST-1",
    "dates": [{"date": "1902-05-03", "type": "Creation"}, {"date": "1902", "type": "Accumulation"}],
    "scope_and_content": "Letter from Jan Pretorius to his sister Anna, Pretoria, 3 May 1902.",
    "archival_history": "Pretoria",  # hidden, and a word that is on the page elsewhere
    "place_access_points": ["Pretoria", "Secret farm"],
    "name_access_points": ["Jan Pretorius", "Anna Pretorius"],
    "creators": [{"authorized_form_of_name": "Jan Pretorius"}],
    "notes": ["Donor restriction applies to the Engelbrecht papers"],  # on the page, but not under a note heading
}


class StockFields(Base):
    def setUp(self):
        super().setUp()
        self.stock = settings.Catalogue("Stock", "https://stock.example.org", "https://stock.example.org/api", "atom-base", "svc", "REST-API-Key")
        self.token = settings._current.set(self.stock)
        self.cat.routes[("GET", "stock.example.org/rec")] = httpx.Response(200, text=STOCK_PAGE)
        self.cat.json("GET", "stock.example.org/api/informationobjects/rec", STOCK_API)

    def tearDown(self):
        settings._current.reset(self.token)
        super().tearDown()

    def test_shown_fields_pass(self):
        r = catalogue._read_base("rec", None)
        self.assertEqual(r["scope_and_content"], STOCK_API["scope_and_content"])
        self.assertEqual(r["places"], ["Pretoria"])
        self.assertEqual(r["dates"], [{"date": "1902-05-03", "type": "Creation"}])

    def test_a_value_that_appears_elsewhere_on_the_page_is_not_shown(self):
        r = catalogue._read_base("rec", None)
        self.assertIsNone(r["archival_history"])  # "Pretoria" is on the page, but not under Archival history
        self.assertIsNone(r["notes"])  # the note's text is on the page, but not under a note heading

    def test_short_values_and_unlisted_items_are_dropped(self):
        r = catalogue._read_base("rec", None)
        self.assertNotIn({"date": "1902", "type": "Accumulation"}, r["dates"] or [])  # "1902" is on the page, not as this date
        self.assertNotIn("Secret farm", r["places"])
        self.assertEqual(r["names"], ["Jan Pretorius"])  # Anna is not an access point on the page; creators heading absent
        self.assertIsNone(r["reference_code"])  # no Reference code heading on this page

    def test_only_the_whole_value_counts(self):
        sections = {"Scope and content": "Letter from Jan Pretorius to his sister Anna"}
        self.assertIsNone(catalogue._shown("Letter from Jan Pretorius to his sister Anna, and a private remark", sections["Scope and content"]))

    def test_hidden_record_is_not_found(self):
        self.cat.routes[("GET", "stock.example.org/rec")] = httpx.Response(302, headers={"location": "/user/login"})
        with self.assertRaises(catalogue.NotVisible):
            catalogue._read_base("rec", None)


class Proposals(Base):
    def test_public_service_cannot_propose(self):
        r = server.propose_description_change("x", "scope_and_content", "New text")
        self.assertIn("needs you to be signed in", r["message"])
        self.assertEqual([q for q in self.cat.seen if q.method == "POST"], [])

    def test_hourly_cap_is_kept_on_disk(self):
        self.assertEqual(calls._budget("k1", 3), 2)
        # A second connection to the same file, as after a restart.
        import sqlite3
        db = sqlite3.connect(os.path.join(TMP, "proposal-cap.db"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM spent").fetchone()[0], 3)
        with self.assertRaises(catalogue.Refused):
            calls._budget("k1", 3)
        self.assertEqual(calls._budget("k1"), 2)  # a refusal spends nothing
        self.assertEqual(calls._budget("k2", 5), 0)  # each key has its own hour


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


class _Connected:
    """What httpx reports as a response's network stream: the address connected to."""
    def __init__(self, ip):
        self.ip = ip

    def get_extra_info(self, name):
        return (self.ip, 443) if name == "server_addr" else None


class Rebinding(Base):
    def setUp(self):
        super().setUp()
        self.demo = settings.Catalogue("Demo", "https://demo.example.net", "https://demo.example.net/api/v2", "atom-ahg", "k", "X-API-Key", demo=True)
        self.token = settings._current.set(self.demo)

    def tearDown(self):
        settings._current.reset(self.token)
        super().tearDown()

    def _answer(self, ip, path="demo.example.net/api/v2/descriptions/x", **kw):
        self.cat.routes[("GET", path)] = httpx.Response(200, json={"title": "T"}, extensions={"network_stream": _Connected(ip)}, **kw)

    def test_demo_answer_from_a_private_address_is_refused(self):
        self._answer("10.0.0.7")
        with self.assertRaises(catalogue.Refused) as e:
            catalogue._api("GET", "/descriptions/x")
        self.assertIn("non-public address", e.exception.message)

    def test_demo_answer_from_a_public_address_is_used(self):
        self._answer("169.1.190.139")
        self.assertEqual(catalogue._api("GET", "/descriptions/x")["title"], "T")

    def test_demo_image_from_a_private_address_is_refused(self):
        self.cat.routes[("GET", "demo.example.net/x.png")] = httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}, extensions={"network_stream": _Connected("127.0.0.1")})
        catalogue._not_public, real = (lambda url: None), catalogue._not_public  # the name looked public when checked
        try:
            with self.assertRaises(catalogue.ImageRefused) as e:
                catalogue._fetch_image("https://demo.example.net/x.png")
        finally:
            catalogue._not_public = real
        self.assertEqual(e.exception.outcome, "off_site")

    def test_configured_catalogues_are_not_checked(self):
        settings._current.reset(self.token)
        self.token = settings._current.set(settings.HOME)
        self.cat.routes[("GET", "cat.example.org/api/v2/descriptions/x")] = httpx.Response(
            200, json={"title": "T"}, extensions={"network_stream": _Connected("10.0.0.7")})
        self.assertEqual(catalogue._api("GET", "/descriptions/x")["title"], "T")  # an operator's own choice, e.g. a LAN catalogue


class Health(Base):
    def _health(self, response):
        if response is not None:
            self.cat.routes[("GET", "cat.example.org/api/v2/descriptions")] = response
        import asyncio
        r = asyncio.run(server.healthz(None))
        return r.status_code, json.loads(r.body)["status"]

    def test_states(self):
        self.assertEqual(self._health(httpx.Response(200, json={"data": {"results": []}})), (200, "ok"))
        self.assertEqual(self._health(httpx.Response(403, json={"message": "Read scope required"})), (503, "key_refused"))
        self.assertEqual(self._health(httpx.Response(500, text="boom")), (503, "not_working"))

    def test_unreachable(self):
        def down(request):
            raise httpx.ConnectError("no route")
        catalogue._client = httpx.Client(transport=httpx.MockTransport(down))
        self.assertEqual(self._health(None), (503, "unreachable"))


class DataFolder(unittest.TestCase):
    def test_signed_in_service_needs_a_writable_data_folder(self):
        import subprocess
        env = {**os.environ, "GLAM_AUTH": "oauth", "GLAM_DATA": "/nonexistent/folder", "MCP_PUBLIC_URL": "https://mcp.example.org",
               "MCP_SECRET": "x"}
        r = subprocess.run([sys.executable, "-c", "import glam_mcp.calls"], env=env, capture_output=True, text=True,
                           cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GLAM_DATA must be a writable folder", r.stderr)


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
