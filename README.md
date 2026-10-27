# AHG Archives MCP server

An MCP server for AtoM catalogues, stock AtoM and AtoM with the AHG Extensions (Heratio is planned). It lets an assistant work with a catalogue the way an archivist would: find records, read them with their children, images, text and provenance, and propose improvements. Nothing it proposes changes a record until an archivist reviews and approves it.

In development. See it in action on AtoM: [AtoM MCP Server in Action](https://youtu.be/n6snV-lgH_Y).

## Try it

- **Without a key:** connect any MCP client to `https://mcp.theahg.co.za/public/mcp`. Read-only, published records of the PSIS demonstration catalogue.
- **With a personal read-only key:** for testing on your own agent, ask support@theahg.co.za for a key (name, organisation, what you want to try, for how long). It reads what a visitor sees, has an end date and a daily limit, and works with `https://mcp.theahg.co.za/mcp`. Suggesting needs a staff key.
- Every key has an hourly limit; each person may make at most 200 suggestions an hour. A used-up limit answers in plain words with the time it resets.

The quick-start guide (`docs/AHG_Archives_MCP_Quick_Start.md`) covers connecting browser and desktop clients, good questions, and suggesting improvements.

## Tools

| Tool | What it does |
| --- | --- |
| `list_catalogues` | The catalogues this service can read: the home one, and any others or demo catalogues. |
| `search_records` | Search descriptions by text, optionally under one parent. `catalogue="all"` searches every catalogue at once. |
| `get_record` | One description in full: dates, creators, access points, hierarchy, children, images and the provenance chain. |
| `get_children` | The records directly below a description. |
| `work_through_series` | Walk every record below a parent (each page of a family Bible), a batch at a time, with your proposals on each so far and how many you may still make this hour. |
| `get_authority` | An authority record (person, family or organisation). |
| `search_authorities` | Find existing people, organisations, families or places by name, with their ids. |
| `get_iiif_manifest` | The IIIF manifest for a record's images. |
| `get_record_text` | Text held for a record's files: extracted text, OCR and transcriptions, paged. |
| `get_record_image` | The record's reference image, for the assistant to look at. |
| `list_my_proposals` | Your proposals and what became of them (pending, approved, linked, rejected...), so rejected ones are not repeated. Signed-in only. |
| `propose_access_points_and_dates` | Propose people, organisations, places and dates, each with its evidence and sources. People and organisations may carry dates of existence, a history and places for a new authority record. A name or place may point at an existing record (`existing_id`), which the archivist sees as the first candidate to link. |
| `propose_relationships` | Propose relationships a record states between people or organisations (spouse, parent, child and the other AtoM relation types), with dates and the line that states them; approved ones show on both authority records. |
| `propose_description_change` | Propose new text for one field of a description (title, scope and content, archival history and others). |
| `propose_transcription` | Propose a transcription of a record's file (a scanned page with no text), reviewed beside the page image; once approved it is the file's text and searchable. |
| `propose_translation` | Propose a translation of a field into another language (e.g. Dutch or Afrikaans into English). |

Every read tool takes an optional `catalogue` (a name from `list_catalogues`); empty means the home catalogue. Proposals always go to the home catalogue.

A failure always answers `found: false` with a `message` for people and an `error` code for programs: `not_found`, `refused`, `rate_limited`, `unsupported`, `unavailable`, `needs_sign_in`, `unknown_catalogue` or `no_image`.

What a caller sees follows AtoM's own permissions: unpublished records, hidden children and master files stay hidden from callers who could not see them in AtoM. `tests/no_leak.py` checks this against a live catalogue.

## Security

- **Reads go through the catalogue's API only**, never its database, and follow its own permissions. A caller with no key of their own reads with the service key, so that key must read no more than a visitor does: the server checks it at start-up (`glam_mcp/keycheck.py`) and refuses to start with a staff key or one that reads records a visitor cannot open. The check samples the newest records; `tests/no_leak.py` with database access is the full test.
- **Nothing is written to a record by the assistant.** Proposals go to the catalogue's review queues; an archivist approves each one. A unit test reads the code and fails if any call other than a search or the review-queue call could be sent; on the catalogue side the proposal endpoint writes only review-queue tables and needs edit rights on the record (see `docs/EVIDENCE.md`). A caller's own key is used only with the home catalogue and is never sent to another.
- **Images** are fetched only from the catalogue's own host and port (a file link must be a plain path), without following redirects, image types only, read in pieces and stopped at 3 MB. A demo catalogue's host must also be a public address.
- **Stock AtoM** returns fields through its API that the site hides from visitors (it does not check draft status, the visible-elements settings or archivist's notes), so on stock AtoM no field is taken from the API at all: every value comes from the record's public page, which AtoM itself rendered for a visitor after its own access rules. The API is used only to find records. This depends on AtoM 2.10's page layout, which the tests fix; AtoM with the AHG Extensions, Heratio and OpenRiC apply visibility in their APIs.
- **Demo catalogues** are added by staff on a web page, so their address is checked when added and again on every connection: an answer that comes from a non-public address (the name now pointing inside a network) is refused before it is used.
- **Limits:** each catalogue key has its own hourly limit, and may have a daily cap and an end date (set in the catalogue); each signed-in person may queue at most `GLAM_PROPOSAL_CAP` proposal items an hour, kept on disk.
- **Logs** keep the tool, catalogue and outcome, but free text (search words, names, proposed text) only as its length and a short hash; owner-only files, deleted after `GLAM_LOG_DAYS` (90). Sign-in tokens are stored as hashes and catalogue keys encrypted.

Visibility on stock AtoM is remembered for five minutes per record (a deliberate choice for speed): a record unpublished there can still appear in search results for up to five minutes, though a full read checks again at once. On AtoM with the AHG Extensions there is no such delay.

`/healthz` answers `ok` (200) only when the catalogue answers a real read with the service key, and otherwise `unreachable`, `key_refused` or `not_working` (503).

Two independent source reviews (26 October 2026) shaped v0.5.0 to v0.5.2. The unit tests, the leak test and the leak test through a second catalogue run in the project's image on every push; each release tag publishes the image only after they pass (GitHub Actions, workflows `no-leak` and `image`). Sign-in through a real browser has its own test (`tests/SIGNIN_TEST.md`, about 10 minutes, then `tests/signin_check.py`); its first run is pending. Still open: the stock AtoM field filter is by text match on the public page, so staff with their own key also see visitor fields only on stock AtoM.

## Code layout

| File | What it holds |
| --- | --- |
| `glam_mcp/server.py` | The MCP layer: tools, sign-in and web routes, start-up. |
| `glam_mcp/catalogue.py` | Catalogue adapters: everything that talks to a catalogue (reads for both backends, stock AtoM visibility and fields, images, the stock CSV). A new backend, such as Heratio, goes here. |
| `glam_mcp/calls.py` | Per-call policy: whose key a call uses, the hourly proposal limit, the call log line. |
| `glam_mcp/settings.py` | Settings, the call log, and the catalogues (home, configured, demo). |
| `glam_mcp/keycheck.py` | Backend detection, the service-key check, the public-address check. |
| `glam_mcp/auth.py` | OAuth sign-in. |
| `glam_mcp/usage.py`, `glam_mcp/catalogues.py` | The staff pages `/usage` and `/catalogues`. |

## Backends

The server finds out which of these a catalogue has when it starts (`GLAM_BACKEND=auto`), so the same install serves both.

- **`atom-ahg`**: AtoM with the AHG Extensions, through REST API v2 (`ahgAPIPlugin`). Needs atom-ahg-plugins v3.121.63 or later for `propose_transcription` and `propose_translation`, v3.121.62 for `propose_relationships`, v3.121.61 for `list_my_proposals`, v3.121.60 for `search_authorities` and `existing_id`, v3.121.45 for authority details on proposals (v3.121.43 for the rest). Proposals are queued in AI > NER review and AI > Suggest review for an archivist; with the Authority Resolution plugin they also reach its queue.
- **Heratio**: served by the `atom-ahg` backend unchanged (Heratio v1.160.0 to v1.163.0, on heratio-dev; checked there with all 17 tools and the leak test, 60 hidden records including embargo, classification and ICIP, 299 checks). Heratio's reads are visitor-level for every key, staff included, so a proposal on a draft answers "not found".
- **`openric`**: OpenRiC's RiC-O JSON-LD API (`/api/ric/v1`), read only; serves both the OpenRiC reference service and OpenRiC Workspace (UUID keys, creators and children inside each record). Records, agents, places and relations; search by title (OpenRiC has no full-text search). OpenRiC Workspace (v0.0.49 and later) is a full backend: file text and reference images from `/records/{uuid}/files` (never masters), every kind of proposal into its review queue (Manage > Proposals to review), and `list_my_proposals`; relationships are sent as RiC relation types and need both sides to be existing authority records. The reference service keeps no files or review queue, so there proposals come back for a person to apply. Detected before the AHG API, which OpenRiC also carries a copy of. OpenRiC shows published records only since its v0.17.26. It needs no key: leave `CATALOGUE_API_KEY` empty, or give an OpenRiC Workspace API key (since Workspace v0.0.46), which reads only what the public may; Workspace refuses a key it does not know. Leak test: `tests/no_leak.py` with `TEST_BACKEND=openric`, or `HIDDEN_FILE` for Workspace.
- **`atom-base`**: stock AtoM, through its own REST API (`arRestApiPlugin`), tested on AtoM 2.10.2. Search, records, children and `work_through_series` work; authority records, IIIF manifests and file text are not offered by the stock API and say so. Proposals are not saved: the stock write API overwrites rather than adds. Instead, names and places come back as a one-row CSV for AtoM's own **Import > CSV** (type *Archival description*, update behaviour *Update matches ignoring blank fields in CSV*, tick *Skip unmatched records*), which adds them to the record and leaves the rest as it is. Dates are listed for entry by hand, because an import replaces the record's dates. AtoM matches the file on the record's identifier, title and repository, so a record without a repository cannot be updated this way.

### Stock AtoM: setting up

1. Admin > Plugins: enable **arRestApiPlugin**.
2. Create a user with **no groups** for the public service and generate its REST API key (edit the user). Signed-in archivists use their own keys.
3. Set `CATALOGUE_URL`, `CATALOGUE_API_KEY` and `CATALOGUE_NAME`; `GLAM_BACKEND=auto` finds the stock API.

AtoM 2.10.2's REST API checks only the "read" permission, not "view draft", so any key can read draft records through it, and it returns fields the site hides from visitors. The server therefore treats a record on stock AtoM as visible only when a visitor can open its public page, which applies the catalogue's own rules (anything else is "not found"), and passes on only the fields that page shows. Keep the API key away from anything but this server.

## Install for your own catalogue

The server runs beside your catalogue, on your own address, and talks to it only through its API. Nothing points at AHG's hosted service unless you set it to.

1. On the catalogue, create a read-only user for the public endpoint and give it an API key. People who will propose changes use their own staff keys.
2. Copy `docker-compose.example.yml` to `docker-compose.yml` and replace every `example.org` address with yours.
3. Put the secrets in `.env` (never commit it):

   ```bash
   cat > .env <<'EOF'
   CATALOGUE_API_KEY=<read-only service user's API key>
   MCP_SECRET=<Fernet key>
   EOF
   ```

   Generate `MCP_SECRET` with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
4. Start it: either pull the ready-made image (`docker pull ghcr.io/archiveheritagegroup/ahg-archives-mcp:latest`, and set `image:` in `docker-compose.yml` to it) or build your own (`docker build -t ahg/glam-mcp:phase2 .`); then `docker compose up -d` and check `curl 127.0.0.1:8031/healthz`. If the service key reads more than a visitor, the services refuse to start and say why (`docker compose logs`).
5. Check the service user's key reads only what a visitor sees: `docker run --rm ghcr.io/archiveheritagegroup/ahg-archives-mcp python -m glam_mcp.keycheck https://catalogue.example.org <key>`.
6. Put the two endpoints behind HTTPS on your own name. `docs/nginx-mcp.theahg.co.za.conf` is a working example to copy and rename.
7. Run `tests/no_leak.py` against your catalogue before opening it to anyone.

A required setting that is missing stops the server at start with its name, rather than falling back to someone else's catalogue.

| Setting | Required | Meaning |
| --- | --- | --- |
| `CATALOGUE_URL` | Yes | The catalogue's public address, used in links. |
| `CATALOGUE_NAME` | Yes | How answers name the catalogue. |
| `CATALOGUE_API_URL` | No | The catalogue's API. Defaults to `CATALOGUE_URL` + `/api/v2` (`/api` for `atom-base`). |
| `CATALOGUE_API_KEY` | Public endpoint | Key of a read-only service user. |
| `GLAM_BACKEND` | No | `auto` (default: asks the catalogue at start-up and uses the AHG API where it is installed, AtoM's own REST API where not), `atom-ahg` or `atom-base`. |
| `GLAM_AUTH` | No | `off` (public, read-only, the default) or `oauth` (signed in). |
| `MCP_PUBLIC_URL` | With `oauth` | This server's own public address, used for sign-in. |
| `MCP_SECRET` | With `oauth` | Fernet key that encrypts stored catalogue keys. |
| `MCP_SIGNED_IN_URL` | No | Where the public endpoint tells people to sign in to propose changes. |
| `GLAM_HTTP_PATH` | No | Endpoint path, e.g. `/mcp` or `/public/mcp`. |
| `GLAM_DATA` | Signed-in service | Folder for the sign-in store (tokens hashed, keys encrypted), the hourly proposal limit and demo catalogues. Mount it on both services; the signed-in service will not start without it. |
| `GLAM_CALL_LOG` | No | Optional JSON-lines log of tool calls: tool, catalogue, outcome, and for free text (search words, names, proposed text) only its length and a short hash. Readable by the service's user only, one file a day. |
| `GLAM_LOG_DAYS` | No | Days of call logs kept before deletion (default 90). |
| `GLAM_PROPOSAL_CAP` | No | Proposal items one signed-in person may queue per hour (default 200). |
| `GLAM_OTHER_CATALOGUES` | No | Further catalogues to read, as JSON (see Several catalogues). |
| `GLAM_DEMO_DAYS` | No | Days a demo catalogue added at `/catalogues` lasts (default 7). |
| `GLAM_USAGE_LOGS` | No | Signed-in service only: comma-separated call logs for the staff usage page at `/usage` (default: its own log). Add the public service's log to see both. |

The older names (`PSIS_PUBLIC_BASE`, `PSIS_API_BASE`, `PSIS_API_KEY`, `GLAM_SOURCE_LABEL`, `GLAM_PUBLIC_BASE`, `GLAM_SECRET`) are still accepted.

`docs/AHG_Archives_MCP_Quick_Start.md` is the guide for archivists using AHG's hosted service: connecting, checking it works, asking good questions and reviewing proposals.

## Several catalogues

One service can read more than one catalogue. The home catalogue (`CATALOGUE_URL`) gets proposals and signed-in users' keys. Others are read only, each with its own service key (make it for a user with no groups, so only what a visitor sees comes back), and never receive a caller's key or a proposal:

    GLAM_OTHER_CATALOGUES='[{"name": "Heratio", "url": "https://heratio.example.org", "key": "..."}]'

A catalogue that cannot be reached at start-up is left out with a warning. Each entry may set `"backend"` instead of having it detected.

**Demo catalogues.** For a show and tell, catalogue administrators and editors can add a client's catalogue on the signed-in service's `/catalogues` page: a name, its address and a read-only key from it. Agents then find it in `list_catalogues` and read it with `catalogue="<name>"`. It lapses after `GLAM_DEMO_DAYS` (7) days. Addresses inside the host's own network are refused. Both services read the list from `<GLAM_DATA>/demo-catalogues.json`, so they share it without a restart.

**Federation.** `search_records` with `catalogue="all"` searches every catalogue at once, each with its own visitor key, and labels every result with its catalogue. To search a catalogue's federation peers (ahgFederationPlugin), add each peer here as its own catalogue with a visitor key from that peer. The service does not search through the plugin's federation: the plugin queries its peers with whatever key each was set up with, and its Elasticsearch connector does not leave out unpublished records, so results could include what a visitor may not see. Added this way, every peer passes the start-up key check and can be leak-tested.

To run the no-leak test through a second catalogue, set `TEST_CATALOGUE` to its name.

## Running it

`docs/OPERATIONS.md`: update, back up and restore (`hosting/backup.sh`), roll back, and watch (`hosting/healthcheck.sh`, a Workbench notification when a service stops answering). `SECURITY.md`: how to report a security problem.

Every request to a catalogue carries the User-Agent `AHG-GLAM-MCP/0.7 (+https://www.atom-extensions.com)`. To let the service past fail2ban or bot rules, match on the prefix `AHG-GLAM-MCP/`, which stays the same between versions; the hosted service calls from 169.1.190.139 (theahg.ddns.net).

Tools run in worker threads, so one slow catalogue does not hold other callers up. Load test (`tests/load_test.py`, against a test catalogue, 26 Oct 2026): 25 clients at once, 40 calls a second, no errors, median record read 0.6 s, memory under 60 MB.

## Ready-made image

Each version tag (`v0.4.0` and on) runs the no-leak tests and then publishes `ghcr.io/archiveheritagegroup/ahg-archives-mcp`, tagged with the version and `latest` (`.github/workflows/image.yml`). Pin a version in production.

## Hosted clients

`hosting/client.sh` runs the service for other institutions' catalogues on one host, one pair of services (signed-in and public) per client, each with its own address, sign-in secret, logs, usage page and demo catalogues:

    hosting/client.sh add <client> "<Catalogue name>" <catalogue-url> [host]   # asks for the client's service key
    hosting/client.sh leak-test <client> "<note>"   # record that tests/no_leak.py passed against their catalogue
    hosting/client.sh publish <client>    # nginx site and certificate (sudo), once DNS resolves and a leak test is recorded
    hosting/client.sh check <client>      # health, and the key check again
    hosting/client.sh list
    hosting/client.sh remove <client>     # stops it and deletes its key, secret, data and logs

The host defaults to `<client>.mcp.theahg.co.za`, ports are taken from 8041 up, and each client's settings live in `clients/<client>/.env` (mode 600, not in git). `GLAM_IMAGE` picks the image.

Before anything starts, the client's key is checked (`glam_mcp/keycheck.py`): a key that may list everyone's proposals is a staff key and is refused, and a sample of the newest records it reads must each open for a visitor on the public site. That is a spot check. The full check is `tests/no_leak.py` with the catalogue's database, run with the client: `publish` refuses until its result is recorded with `leak-test`. The same key check guards the demo catalogues page, where catalogues are read-only and listed for seven days.

## Usage figures

The signed-in service serves a staff page at `/usage`: calls per day, per tool, per service, per outcome and per key over the last 30 days, and what became of proposals (pending, accepted, rejected, by kind). It opens for a catalogue key that may list everyone's proposals (administrators and editors); the key is used for that page only. Keys appear as the first 8 characters of a hash. Point `GLAM_USAGE_LOGS` at both services' logs, and route `/usage` to the signed-in service in nginx (see `docs/nginx-mcp.theahg.co.za.conf`).

## Tests

`tests/test_server.py` and `tests/test_auth.py` (74 tests) run against a fake catalogue, with no network: every read tool's answers and refusals (401, 403, 404, 429, 501, 500), catalogue choice and keys, image fetching (own host only, no redirects, image types only, size limit), demo catalogues answering from a non-public address, stock AtoM reading only from the public page (nothing that only the API returns comes through), the OpenRiC adapter, same-host redirects in the key check, that no call besides search and the review queue can be sent, the health states, the data folder rule, the hourly proposal limit, the key check, log contents, and sign-in (hashed tokens, encrypted keys, single-use codes, rotation, revocation, a key revoked at the catalogue). CI runs them on every push:

    docker run --rm -v "$PWD/tests:/app/tests:ro" ahg/glam-mcp:phase2 python -m unittest discover -s tests -p 'test_*.py'

`tests/no_leak.py` checks that hidden records (drafts and, with the AHG extensions, embargoed, classified and ICIP-restricted records) never come back through any tool: not by reference, not in search, not among children, not named in a visible record's hierarchy. On every push, GitHub Actions starts a throwaway stock AtoM 2.10.2 with published and draft records (`tests/stock-atom`), runs the public service against it and runs the test (`.github/workflows/no-leak.yml`). To run it by hand:

    cd tests/stock-atom && docker compose -p stock-atom up -d --build
    docker compose -p stock-atom exec -T atom sh /opt/setup.sh    # prints READER_API_KEY and ADMIN_API_KEY
    # start the server with CATALOGUE_URL=http://127.0.0.1:8097 and the reader key, then:
    GLAM_MCP_URL=http://127.0.0.1:8000/mcp CATALOGUE_DB_CNF=db.cnf CATALOGUE_DB=atom PUBLIC_CONTROL_SLUG=published-letter python tests/no_leak.py

`docker compose -p stock-atom down -v` removes the test catalogue.

## Versions

- **v0.9.2** (27 Oct 2026): every tool declares the four MCP tool annotations, so a client can tell reads from writes: the eleven read tools are read-only and idempotent; the five propose tools are not read-only, never destructive (a proposal only joins the review queue) and not idempotent. None is open-world. 79 unit tests.
- **v0.9.1** (27 Oct 2026): rights and Local Contexts labels from AtoM with the AHG Extensions too (atom-ahg-plugins v3.121.89, the API rights block; Heratio to follow), in get_record as well as with files; rights the catalogue cannot read withhold the file. 78 unit tests.
- **v0.9.0** (27 Oct 2026): rights and Local Contexts labels from OpenRiC Workspace (v0.0.59): RightsStatements.org statements, Creative Commons licences, PREMIS acts and TK/BC labels come with get_record_text and get_record_image. A file under a no-reuse label (TK Secret/Sacred, Community Use Only, Women or Men Restricted), or one that may not be rendered, disseminated or used, is withheld: the assistant gets the rights, not the content. 74 unit tests.
- **v0.8.2** (26 Oct 2026): stock AtoM children and children_count come from the visitor's treeview (direct children, paged, drafts left out) instead of the REST API tree, which returns the whole subtree; a large fonds no longer times out or ties up the catalogue. 69 unit tests.
- **v0.8.1** (26 Oct 2026): from an outside tester's retest on stock AtoM: get_children answers a draft and a missing slug alike (visibility checked before the API); records carry part_of from the visitor's breadcrumb and children_count of visible children. 68 unit tests.
- **v0.8.0** (26 Oct 2026): OpenRiC Workspace as a full backend: file text and images, proposals into its review queue, my proposals, relations as RiC types, UUID links; a reviewer's key is refused as service key; a catalogue's reason for refusing a request is passed on. Key check waits out rate limiting; OpenRiC needs no service key. 64 unit tests.
- **v0.7.1** (26 Oct 2026): one User-Agent on every request, `AHG-GLAM-MCP/0.7`, with a prefix that stays the same for fail2ban and bot rules; the health check covers every hosted client; the leak test runs against OpenRiC (Workspace passed).
- **v0.7.0** (26 Oct 2026): evidence release, second part. Tools run in worker threads (3 times the throughput under load); an `error` code on every failure; OpenRiC Workspace through the openric adapter; dependency scan and SBOM in CI (pip upgraded in the image); `SECURITY.md`; `docs/OPERATIONS.md` with backup, restore, rollback and health alerts, each tried once; `list_catalogues` logged; hosted clients removed completely on leaving. 52 unit tests.
- **v0.6.1** (26 Oct 2026): OpenRiC adapter for OpenRiC v0.17.27, which shows published records only: dates from its date ranges, children from its hierarchy, typed title search, key check through its API. Checked locally against ric.theahg.co.za.
- **v0.6.0** (26 Oct 2026): evidence release. Stock AtoM fields only from the public page (no field from the API); OpenRiC adapter (read only); one table of backends; Heratio served by `atom-ahg`; key check follows same-host redirects; leak test covers Heratio's embargo table, classification above PUBLIC, inherited restrictions, and samples each kind of hidden record; a unit test proves no write path besides the review queue. 45 unit tests.
- **v0.5.2** (26 Oct 2026): after a second review. Stock AtoM fields matched as whole values under their own heading only; demo catalogues checked against the address actually connected to (DNS rebinding); the signed-in service requires a writable data folder; `/healthz` tells unreachable, key refused and not working apart; hosted clients need a recorded leak test before going public. 39 unit tests.
- **v0.5.1** (26 Oct 2026): `server.py` split into the MCP layer, catalogue adapters, per-call policy and settings (no change in behaviour); sign-in test through a real browser (`tests/SIGNIN_TEST.md`, `tests/signin_check.py`).
- **v0.5.0** (26 Oct 2026): security and reliability release after an independent source review. Image fetching bound to the catalogue's own host; service-key check at start-up; stock AtoM fields only as the public page shows them; logs without free text, rotated and deleted after 90 days; hourly proposal limit kept on disk; unit tests (fake catalogue) in CI.
- **v0.4.0** (26 Oct 2026): several catalogues (`list_catalogues`, `catalogue` on every read tool, search across all), demo catalogues page, staff usage page, hosted clients (`hosting/client.sh`), ready-made image, key check.

## Licence

Copyright (C) 2026 Johan Pieterse, Plain Sailing Information Systems. Released under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later); see `LICENSE`. Anyone who runs a changed version as a network service must offer its users the changed source.

## Contact

The Archive and Heritage Digital Commons Group - support@theahg.co.za
