# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool, error, catalogue and safety tests against a fake catalogue (no network).

Run in the image, where the service's own libraries are:
    docker run --rm -v "$PWD/tests:/app/tests:ro" ahg/glam-mcp:phase2 python -m unittest discover -s tests -v
"""

import functools
import json
import os
import re
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
    """Answers by (method, path); records every request it is sent. A list of
    answers is given in turn, the last one repeating."""

    def __init__(self):
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        hit = self.routes.get((request.method, request.url.host + request.url.path))
        if isinstance(hit, list):
            hit = hit.pop(0) if len(hit) > 1 else hit[0]
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


STOCK_PAGE = "<html><body><h1>Item TEST-1 - Letter of 1902 about Pretoria</h1>" + "".join([
    _field("Reference code", "TEST-1"),
    _field("Title", "Letter of 1902 about Pretoria"),
    _field("Date(s)", '<ul class="m-0"><li> 1902-05-03 (Creation) </li></ul>'),
    _field("Level of description", "Item"),
    _field("Name of creator", '<div class="creator"> <a href="/jan" title="Jan Pretorius">Jan Pretorius</a> </div>'),
    _field("Scope and content", "<p>Letter from Jan Pretorius to his sister Anna, Pretoria, 3 May 1902.</p>"),
    _field("Archival history", ""),  # hidden from visitors by the visible-elements settings: shown empty
    _field("Place access points", '<ul><li> <a href="/pretoria">Pretoria</a> </li></ul>'),
    _field("Name access points", '<ul><li><a href="/anna">Anna Pretorius</a><span class="note2"> (Subject)</span></li></ul>'),
]) + "<h2>Explore</h2><p>Donor restriction applies to the Engelbrecht papers</p></body></html>"

# What the stock REST API returns for the same record: more than the page shows.
STOCK_API = {
    "title": "Letter of 1902 about Pretoria", "reference_code": "TEST-1", "publication_status": "Draft",
    "archival_history": "Bought from a private dealer in 1987", "place_access_points": ["Pretoria", "Secret farm"],
    "notes": ["Donor restriction applies to the Engelbrecht papers"],
}


class StockFields(Base):
    """Stock AtoM: every value comes from the public page; nothing from the API."""

    def setUp(self):
        super().setUp()
        self.stock = settings.Catalogue("Stock", "https://stock.example.org", "https://stock.example.org/api", "atom-base", "svc", "REST-API-Key")
        self.token = settings._current.set(self.stock)
        self.cat.routes[("GET", "stock.example.org/rec")] = httpx.Response(200, text=STOCK_PAGE)
        self.cat.json("GET", "stock.example.org/api/informationobjects/rec", STOCK_API)

    def tearDown(self):
        settings._current.reset(self.token)
        super().tearDown()

    def test_fields_as_the_page_shows_them(self):
        r = catalogue._read_base("rec", None)
        self.assertEqual((r["title"], r["reference_code"], r["level"]), ("Letter of 1902 about Pretoria", "TEST-1", "Item"))
        self.assertEqual(r["scope_and_content"], "Letter from Jan Pretorius to his sister Anna, Pretoria, 3 May 1902.")
        self.assertEqual(r["dates"], [{"date": "1902-05-03", "type": "Creation"}])
        self.assertEqual(r["names"], ["Jan Pretorius", "Anna Pretorius"])  # creator, then access point without "(Subject)"
        self.assertEqual(r["places"], ["Pretoria"])

    def test_nothing_only_the_api_returns_comes_through(self):
        r = catalogue._read_base("rec", None)
        self.assertIsNone(r["archival_history"])  # empty on the page, filled in the API
        self.assertNotIn("Secret farm", r["places"])
        self.assertIsNone(r["notes"])  # the text is on the page, but not under a note heading
        self.assertNotIn("Draft", json.dumps(r))
        self.assertFalse([q for q in self.cat.seen if "/api/" in q.url.path])  # the API is not even asked

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
        import hashlib
        k1 = hashlib.sha256(b"k1").hexdigest()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM spent WHERE key = ?", (k1,)).fetchone()[0], 3)
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

    def test_same_host_redirects_are_followed_others_are_not(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"message": "Forbidden"}, 403)
        self.cat.json("GET", "cat.example.org/api/v2/descriptions", {"data": {"results": [{"slug": "a-vase"}, {"slug": "a-draft"}, {"slug": "elsewhere"}]}})
        self.cat.routes[("GET", "cat.example.org/a-vase")] = httpx.Response(302, headers={"location": "/museum/a-vase"})
        self.cat.routes[("GET", "cat.example.org/museum/a-vase")] = httpx.Response(200, text="<html>")
        self.cat.routes[("GET", "cat.example.org/a-draft")] = httpx.Response(302, headers={"location": "/user/login?next=/a-draft"})
        self.cat.routes[("GET", "cat.example.org/elsewhere")] = httpx.Response(302, headers={"location": "https://other.example/x"})
        self.cat.routes[("GET", "other.example/x")] = httpx.Response(200, text="<html>")
        self.assertIn("2 of 3 sampled records", keycheck.check(HOME, "k", "atom-ahg"))

    def test_rate_limited_record_is_retried_not_counted_hidden(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"message": "Forbidden"}, 403)
        self.cat.json("GET", "cat.example.org/api/v2/descriptions", {"data": {"results": [{"slug": "busy-one"}]}})
        self.cat.routes[("GET", "cat.example.org/busy-one")] = [httpx.Response(429, headers={"retry-after": "0"}), httpx.Response(200, text="<html>")]
        self.assertIsNone(keycheck.check(HOME, "k", "atom-ahg"))

    def test_catalogue_that_stays_busy_is_not_called_leaking(self):
        self.cat.json("GET", "cat.example.org/api/v2/proposals", {"message": "Forbidden"}, 403)
        self.cat.json("GET", "cat.example.org/api/v2/descriptions", {"data": {"results": [{"slug": "busy-one"}]}})
        self.cat.routes[("GET", "cat.example.org/busy-one")] = httpx.Response(429, headers={"retry-after": "0"})
        self.assertIn("too many requests", keycheck.check(HOME, "k", "atom-ahg"))

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


RIC = "ric.example.org"
RIC_RECORD = {
    "@context": {"rico": "https://www.ica.org/standards/RiC/ontology#"},
    "@id": f"https://{RIC}/informationobject/photo-taken-1936", "@type": "https://www.ica.org/standards/RiC/ontology#Record",
    "rico:type": "Item", "rico:title": "Photo taken 1936", "rico:identifier": "PH-1", "rico:scopeAndContent": "A family on the stoep.",
    "rico:hasOrHadHolder": {"@id": f"https://{RIC}/repository/x", "@type": "rico:CorporateBody", "rico:name": "Example Archive"},
    "rico:hasOrHadSubject": [{"@type": "rico:Thing", "skos:prefLabel": "Families"}, {"@type": "rico:Thing", "skos:prefLabel": "Farms"}],
    "rico:hasOrHadInstantiation": [{"@type": "rico:Instantiation", "rico:identifier": "ph1.jpg"}],
    "openricx:hasDateRangeSet": [{"@type": "openricx:DateRange", "rico:beginningDate": "1936-01-01", "rico:endDate": "1936-12-31",
                                  "rico:hasDateType": {"@id": "https://openric.org/vocab/date-type/creation"}},
                                 {"@type": "openricx:DateRange", "rico:expressedDate": "c. 1936",
                                  "rico:hasDateType": {"@id": "https://openric.org/vocab/date-type/accumulation"}}],
}


class OpenRiC(Base):
    def setUp(self):
        super().setUp()
        self.ric = settings.Catalogue("OpenRiC", f"https://{RIC}", f"https://{RIC}/api/ric/v1", "openric", "", "X-API-Key")
        settings.CATALOGUES = {"Home": settings.HOME, "OpenRiC": self.ric}
        api = f"{RIC}/api/ric/v1"
        self.cat.json("GET", f"{api}/autocomplete", [{"id": 890, "label": "Photo taken 1936", "type": "Record"},
                                                      {"id": 726, "label": "Mobrey family", "type": "Agent"},
                                                      {"id": 999, "label": "A draft", "type": "Record"}])
        self.cat.routes[("GET", f"{api}/records/890")] = httpx.Response(200, json=RIC_RECORD, headers={"content-type": "application/ld+json"})
        self.cat.routes[("GET", f"{api}/records/999")] = httpx.Response(
            404, json={"type": "https://openric.org/errors/not-found", "title": "Not found", "status": 404, "detail": "No such record"})
        self.cat.json("GET", f"{api}/relations-for/890", {"entity_id": 890, "outgoing": [], "total": 2, "incoming": [
            {"target_name": "Mobrey family", "target_type": "Agent", "rico_predicate": "rico:resultsOrResultedFrom"},
            {"target_name": "A deed", "target_type": "Record", "rico_predicate": "rico:hasOrHadPart"}]})
        self.cat.json("GET", f"{api}/records/890/entities", {"places": [{"rico:name": "Graaff-Reinet"}], "rules": []})
        self.cat.json("GET", f"{api}/agents/726", {"@id": f"https://{RIC}/actor/mobrey-family", "@type": "rico:Agent", "rico:name": "Mobrey family"})
        self.cat.json("GET", f"{api}/hierarchy/890", {"entity_id": 890, "class": "QubitInformationObject", "parent": None,
                                                      "children": [{"@id": f"https://{RIC}/informationobject/back", "id": 891, "slug": "back",
                                                                    "title": "Back of the photo", "level": "Item"}]})

    def test_search_cites_each_records_own_page_and_skips_hidden_ones(self):
        r = server.search_records("photo", catalogue="OpenRiC")
        self.assertEqual(r["results"], [{"slug": "890", "title": "Photo taken 1936", "level": "Item",
                                          "url": f"https://{RIC}/informationobject/photo-taken-1936"}])  # no agent, no hidden record

    def test_read_maps_ric_o(self):
        r = server.get_record("890", catalogue="OpenRiC")
        self.assertEqual((r["title"], r["reference_code"], r["scope_and_content"], r["repository"]),
                         ("Photo taken 1936", "PH-1", "A family on the stoep.", "Example Archive"))
        self.assertEqual(r["subjects"], ["Families", "Farms"])
        self.assertEqual((r["names"], r["places"]), (["Mobrey family"], ["Graaff-Reinet"]))
        self.assertEqual(r["url"], f"https://{RIC}/informationobject/photo-taken-1936")
        self.assertEqual(r["dates"], [{"date": "1936-01-01 - 1936-12-31", "type": "creation"}, {"date": "c. 1936", "type": "accumulation"}])

    def test_hidden_record_and_problem_details(self):
        r = server.get_record("999", catalogue="OpenRiC")
        self.assertEqual(r["message"], "No visible record with that reference.")
        self.cat.routes[("GET", f"{RIC}/api/ric/v1/records/890")] = httpx.Response(
            429, json={"type": "about:blank", "title": "Too Many Requests", "status": 429, "detail": "60 requests a minute"})
        self.assertEqual(server.get_record("890", catalogue="OpenRiC")["message"], "60 requests a minute")

    def test_children_agents_and_what_it_does_not_offer(self):
        kid = server.get_children("890", catalogue="OpenRiC")["results"][0]
        self.assertEqual((kid["slug"], kid["title"], kid["url"]), ("891", "Back of the photo", f"https://{RIC}/informationobject/back"))
        server.search_records("photo", catalogue="OpenRiC")
        self.assertEqual([q.url.params.get("types") for q in self.cat.seen if q.url.path.endswith("/autocomplete")][-1], "record")
        self.assertEqual(server.search_authorities("mobrey", catalogue="OpenRiC")["results"][0]["id"], 726)
        self.assertEqual(server.get_authority("726", catalogue="OpenRiC")["name"], "Mobrey family")
        self.assertIn("does not publish file text", server.get_record_text("890", catalogue="OpenRiC")["message"])
        self.assertEqual(server.list_catalogues()["catalogues"][1]["kind"], "OpenRiC")

    def test_detected_before_its_copy_of_an_ahg_api(self):
        self.cat.json("GET", f"{RIC}/api/ric/v1/records", {"@type": "openricx:RecordList", "openric:items": []})
        self.cat.json("GET", f"{RIC}/api/v2/descriptions", {"data": {"results": []}})
        real = keycheck.httpx.get
        keycheck.httpx.get = lambda url, **kw: httpx.Client(transport=httpx.MockTransport(self.cat)).get(url, headers=kw.get("headers"))
        try:
            self.assertEqual(keycheck.detect_backend(f"https://{RIC}", ""), "openric")
        finally:
            keycheck.httpx.get = real

    def test_key_check_needs_every_listed_record_to_open_for_a_visitor(self):
        self.cat.json("GET", f"{RIC}/api/ric/v1/records", {"openric:items": [
            {"@id": f"https://{RIC}/informationobject/public-one"}, {"@id": f"https://{RIC}/informationobject/draft-one"}]})
        self.cat.json("GET", f"{RIC}/api/ric/v1/records/public-one", {"@id": f"https://{RIC}/informationobject/public-one"})
        self.assertIn("1 of 2 sampled records do not open through the API", keycheck.check(f"https://{RIC}", "", "openric"))


class NoWritePath(unittest.TestCase):
    """Nothing the assistant does can change a record: the only calls the code
    sends that are not reads are a search and the review-queue call. Read from
    the code itself, so a new write path fails this test."""

    def test_only_search_and_the_review_queue_are_not_reads(self):
        import ast
        # Proposals are posted to path = _queue_path(slug), which may only name a review queue.
        import inspect
        queues = re.findall(r'return f"([^"]+)"', inspect.getsource(server._queue_path))
        self.assertEqual(queues, ["/descriptions/{slug}/proposals", "/records/{slug}/proposals"])
        allowed = {("POST", "/search"), ("POST", "path")}
        found = set()
        src_dir = os.path.join(os.path.dirname(__file__), "..", "glam_mcp")
        for name in os.listdir(src_dir):
            if not name.endswith(".py"):
                continue
            for node in ast.walk(ast.parse(open(os.path.join(src_dir, name)).read())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "_api" and node.args:
                    method = ast.literal_eval(node.args[0]) if isinstance(node.args[0], ast.Constant) else "?"
                    path = ast.unparse(node.args[1]).strip("f'\"") if len(node.args) > 1 else "?"
                    if method != "GET":
                        found.add((method, path))
                # A raw client call that could write (post, put, patch, delete) outside _api.
                if isinstance(node, ast.Attribute) and node.attr in ("post", "put", "patch", "delete") and \
                        isinstance(node.value, ast.Name) and node.value.id in ("_client", "httpx"):
                    found.add(("raw", f"{name}: {node.value.id}.{node.attr}"))
            text = open(os.path.join(src_dir, name)).read()
            # The one call whose method is a value is inside _api, whose callers are checked above.
            for m in re.finditer(r"\.(request|send)\(", text):
                found.add(("value", f"{name}: .{m.group(1)}(")) if not (name == "catalogue.py" and "def _api(" in text[:m.start()]
                                                                       and text[:m.start()].rfind("\ndef ") == text.find("\ndef _api(")) else None
        self.assertEqual(found - allowed, set(), "a call that is not a read and not the review queue")
        self.assertEqual(found, allowed)


class ErrorCodes(Base):
    """Every failure answers {found: false, error: <code>, message, source}."""

    def test_each_failure_has_its_code(self):
        self.assertEqual(server.get_record("missing")["error"], "not_found")
        for status, code in ((401, "refused"), (403, "refused"), (429, "rate_limited"), (501, "unsupported")):
            self.cat.json("GET", "cat.example.org/api/v2/descriptions/x", {"message": "no"}, status)
            self.assertEqual(server.get_record("x")["error"], code, status)
        self.cat.routes[("GET", "cat.example.org/api/v2/descriptions/x")] = httpx.Response(500)
        self.assertEqual(server.get_record("x")["error"], "unavailable")
        self.assertEqual(server.get_record("x", catalogue="Nope")["error"], "unknown_catalogue")
        self.assertEqual(server.propose_description_change("x", "title", "t")["error"], "needs_sign_in")

    def test_every_failure_shape_in_the_code_carries_a_code(self):
        import ast
        src_dir = os.path.join(os.path.dirname(__file__), "..", "glam_mcp")
        for name in ("server.py", "calls.py"):
            for node in ast.walk(ast.parse(open(os.path.join(src_dir, name)).read())):
                if isinstance(node, ast.Dict):
                    keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                    if "found" in keys and any(isinstance(v, ast.Constant) and v.value is False
                                               for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and k.value == "found"):
                        self.assertIn("error", keys, f"{name}:{node.lineno} answers found: false without an error code")


class Outages(Base):
    def test_timeout_answers_unavailable_without_internals(self):
        def slow(request):
            raise httpx.ReadTimeout("timed out", request=request)
        catalogue._client = httpx.Client(transport=httpx.MockTransport(slow))
        r = server.search_records("x")
        self.assertEqual((r["found"], r["error"]), (False, "unavailable"))
        self.assertNotIn("timed out", json.dumps(r))

    def test_catalogue_down_then_back(self):
        state = {"up": False}
        def flaky(request):
            if not state["up"]:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"data": {"total": 1, "results": [{"slug": "s", "title": "T"}]}})
        catalogue._client = httpx.Client(transport=httpx.MockTransport(flaky))
        self.assertEqual(server.search_records("x")["error"], "unavailable")
        state["up"] = True
        self.assertEqual(server.search_records("x")["results"][0]["slug"], "s")  # no restart, no stuck state


class Concurrency(unittest.TestCase):
    def test_hourly_limit_holds_under_simultaneous_proposals(self):
        import threading
        results, lock = [], threading.Lock()
        def spend():
            try:
                calls._budget("concurrent-key", 1)
                ok = True
            except catalogue.Refused:
                ok = False
            with lock:
                results.append(ok)
        threads = [threading.Thread(target=spend) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), int(os.environ["GLAM_PROPOSAL_CAP"]))  # exactly the cap, never more
        self.assertEqual(results.count(False), 20 - int(os.environ["GLAM_PROPOSAL_CAP"]))


WS = "workspace.example.org"


class OpenRiCWorkspace(Base):
    """OpenRiC Workspace: UUID keys, IRIs from autocomplete, creators and children inside the record."""

    def setUp(self):
        super().setUp()
        self.ws = settings.Catalogue("Workspace", f"https://{WS}", f"https://{WS}/api/ric/v1", "openric", "", "X-API-Key")
        settings.CATALOGUES = {"Home": settings.HOME, "Workspace": self.ws}
        uid, kid = "01a15b79-693f-7204", "01a15b79-6a03-73df"
        self.cat.json("GET", f"{WS}/api/ric/v1/autocomplete", [
            {"id": f"https://{WS}/record/{uid}", "@id": f"https://{WS}/record/{uid}", "label": "Rock shelter survey", "type": "Record"},
            {"id": f"https://{WS}/repository/x", "label": "The British Museum", "type": "Repository"}])
        self.cat.json("GET", f"{WS}/api/ric/v1/records/{uid}", {
            "@id": f"https://{WS}/record/{uid}", "@type": "rico:RecordSet", "rico:title": "Rock shelter survey",
            "rico:identifier": "RSS-1", "rico:scopeAndContent": "Survey notes.",
            "rico:hasCreator": [{"@id": f"https://{WS}/agent/a1", "@type": "rico:Person", "rico:name": "Heinrich Mueller"}],
            "rico:includesOrIncluded": [{"@id": f"https://{WS}/record/{kid}", "@type": "rico:Record", "rico:name": "Panel 6"}]})

    def test_search_uses_the_uuid_and_the_records_own_page(self):
        r = server.search_records("rock", catalogue="Workspace")
        self.assertEqual([(h["slug"], h["url"]) for h in r["results"]], [("01a15b79-693f-7204", f"https://{WS}/record/01a15b79-693f-7204")])

    def test_inline_creators_and_children(self):
        r = server.get_record("01a15b79-693f-7204", catalogue="Workspace")
        self.assertEqual((r["title"], r["names"]), ("Rock shelter survey", ["Heinrich Mueller"]))
        kids = server.get_children("01a15b79-693f-7204", catalogue="Workspace")["results"]
        self.assertEqual(kids, [{"slug": "01a15b79-6a03-73df", "title": "Panel 6", "level": "Record", "url": f"https://{WS}/record/01a15b79-6a03-73df"}])
        self.assertFalse([q for q in self.cat.seen if "/relations-for/" in q.url.path or "/hierarchy/" in q.url.path])


U = "01a15b79-693f-7204-bcdc-ce5f43c6d6fc"


class WorkspaceFull(Base):
    """OpenRiC Workspace as the home catalogue (v0.8): files, text, review queue."""

    def setUp(self):
        super().setUp()
        self.home = settings.Catalogue("WS", f"https://{WS}", f"https://{WS}/api/ric/v1", "openric", "", "X-API-Key", home=True)
        self._home, calls.HOME, server.HOME = calls.HOME, self.home, self.home
        settings.CATALOGUES = {"WS": self.home}
        self._key, calls._caller_key = calls._caller_key, (lambda ctx: "staff-key")
        self.cat.json("GET", f"{WS}/api/ric/v1/records/{U}/files", {"record": U, "title": "Panel 6", "instantiations": [
            {"id": "i1", "title": "Photo", "files": [
                {"id": "f1", "use": "thumbnail", "mime_type": "image/png", "path": "/discover/files/f1"},
                {"id": "f2", "use": "reference", "mime_type": "image/png", "path": "/discover/files/f2"}],
             "text": [{"language": "en", "value": "Two eland, one human figure."}]}]})

    def tearDown(self):
        calls.HOME, server.HOME, calls._caller_key = self._home, self._home, self._key
        super().tearDown()

    def test_text_is_the_published_transcript(self):
        r = server.get_record_text(U)
        self.assertEqual([(t["source"], t["digital_object_id"], t["text"], t["more"]) for t in r["results"]],
                         [("transcript", "i1", "Two eland, one human figure.", False)])
        self.assertEqual(server.get_record_text(U, offset=4, limit=5)["results"][0]["text"], "eland")

    def test_image_is_the_reference_copy_on_its_own_host(self):
        self.cat.routes[("GET", f"{WS}/discover/files/f2")] = httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        out = server.get_record_image(U)
        self.assertEqual(json.loads(out[0])["image"], f"https://{WS}/discover/files/f2")
        self.assertFalse([q for q in self.cat.seen if q.url.path == "/discover/files/f1"])

    def test_record_that_is_not_public_answers_not_found(self):
        self.cat.routes[("GET", f"{WS}/api/ric/v1/records/{U}/files")] = httpx.Response(404, json={"title": "Not found"})
        self.assertEqual(server.get_record_text(U)["error"], "not_found")

    def test_proposals_go_to_the_review_queue_with_uuid_links(self):
        self.cat.json("POST", f"{WS}/api/ric/v1/records/{U}/proposals", {"record": U, "queued": [{"type": "PERSON", "value": "A"}]})
        r = server.propose_access_points_and_dates(U, [server.Proposal(kind="person", value="A", existing_id="01a1-agent", link_reason="same dates")])
        self.assertEqual(r["review_url"], f"https://{WS}/proposals")
        sent = json.loads(next(q for q in self.cat.seen if q.method == "POST").content)
        self.assertEqual((sent["entities"][0]["actor_id"], self.cat.seen[-1].headers["X-API-Key"]), ("01a1-agent", "staff-key"))

    def test_a_transcript_already_held_is_not_proposed_again(self):
        r = server.propose_transcription(U, "Two  eland, one human figure.")
        self.assertFalse(r["queued"])
        self.assertEqual([q for q in self.cat.seen if q.method == "POST"], [])

    def test_a_malformed_item_passes_the_catalogues_reason(self):
        self.cat.json("POST", f"{WS}/api/ric/v1/records/{U}/proposals", {"title": "Bad", "detail": "Relationship 0: subject_agent_id is not an authority record"}, 400)
        r = server.propose_relationships(U, [server.Relationship(subject="A", relation="is the spouse of", object="B")])
        self.assertIn("subject_agent_id", r["message"])

    def test_my_proposals_link_to_the_record_page(self):
        self.cat.json("GET", f"{WS}/api/ric/v1/proposals", {"total": 1, "results": [{"kind": "field", "id": "p1", "record": U, "status": "proposed"}]})
        self.assertEqual(server.list_my_proposals()["results"][0]["url"], f"https://{WS}/record/{U}")

    def test_reference_service_still_has_no_files(self):
        self.assertEqual(server.get_record_text("553")["error"], "unsupported")

    def test_a_reviewer_key_is_refused_as_service_key(self):
        self.cat.json("GET", f"{WS}/api/ric/v1/proposals", {"total": 0, "results": []})
        self.assertIn("reviewer", keycheck.check(f"https://{WS}", "k", "openric"))
