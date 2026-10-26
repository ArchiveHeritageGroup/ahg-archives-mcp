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

What a caller sees follows AtoM's own permissions: unpublished records, hidden children and master files stay hidden from callers who could not see them in AtoM. `tests/no_leak.py` checks this against a live catalogue.

## Security

- **Reads go through the catalogue's API only**, never its database, and follow its own permissions. A caller with no key of their own reads with the service key, so that key must read no more than a visitor does: the server checks it at start-up (`glam_mcp/keycheck.py`) and refuses to start with a staff key or one that reads records a visitor cannot open. The check samples the newest records; `tests/no_leak.py` with database access is the full test.
- **Nothing is written to a record by the assistant.** Proposals go to the catalogue's review queues; an archivist approves each one. A caller's own key is used only with the home catalogue and is never sent to another.
- **Images** are fetched only from the catalogue's own host and port (a file link must be a plain path), without following redirects, image types only, read in pieces and stopped at 3 MB. A demo catalogue's host must also be a public address.
- **Stock AtoM** returns every field through its API, including ones the site hides from visitors, so only fields the record's public page shows are passed on.
- **Limits:** each catalogue key has its own hourly limit, and may have a daily cap and an end date (set in the catalogue); each signed-in person may queue at most `GLAM_PROPOSAL_CAP` proposal items an hour, kept on disk.
- **Logs** keep the tool, catalogue and outcome, but free text (search words, names, proposed text) only as its length and a short hash; owner-only files, deleted after `GLAM_LOG_DAYS` (90). Sign-in tokens are stored as hashes and catalogue keys encrypted.

An independent source review (26 October 2026) shaped v0.5.0. Still open from it: a full test of sign-in through a real browser, and the stock AtoM field filter is by text match on the public page, so staff with their own key also see visitor fields only on stock AtoM.

## Backends

The server finds out which of these a catalogue has when it starts (`GLAM_BACKEND=auto`), so the same install serves both.

- **`atom-ahg`**: AtoM with the AHG Extensions, through REST API v2 (`ahgAPIPlugin`). Needs atom-ahg-plugins v3.121.63 or later for `propose_transcription` and `propose_translation`, v3.121.62 for `propose_relationships`, v3.121.61 for `list_my_proposals`, v3.121.60 for `search_authorities` and `existing_id`, v3.121.45 for authority details on proposals (v3.121.43 for the rest). Proposals are queued in AI > NER review and AI > Suggest review for an archivist; with the Authority Resolution plugin they also reach its queue.
- **Heratio** (planned, heratio#1558): Heratio's REST API v2 has the same shape. Once it gains the proposals and record-text endpoints and returns provenance in the full record, the `atom-ahg` backend serves Heratio too.
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
| `GLAM_DATA` | No | Folder for the sign-in store (tokens hashed, keys encrypted), the hourly proposal limit and demo catalogues. Mount it on both services. |
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

To run the no-leak test through a second catalogue, set `TEST_CATALOGUE` to its name.

## Ready-made image

Each version tag (`v0.4.0` and on) runs the no-leak tests and then publishes `ghcr.io/archiveheritagegroup/ahg-archives-mcp`, tagged with the version and `latest` (`.github/workflows/image.yml`). Pin a version in production.

## Hosted clients

`hosting/client.sh` runs the service for other institutions' catalogues on one host, one pair of services (signed-in and public) per client, each with its own address, sign-in secret, logs, usage page and demo catalogues:

    hosting/client.sh add <client> "<Catalogue name>" <catalogue-url> [host]   # asks for the client's service key
    hosting/client.sh publish <client>    # nginx site and certificate (sudo), once DNS for the host resolves
    hosting/client.sh check <client>      # health, and the key check again
    hosting/client.sh list
    hosting/client.sh remove <client>     # stops it, deletes key and secret, keeps the logs

The host defaults to `<client>.mcp.theahg.co.za`, ports are taken from 8041 up, and each client's settings live in `clients/<client>/.env` (mode 600, not in git). `GLAM_IMAGE` picks the image.

Before anything starts, the client's key is checked (`glam_mcp/keycheck.py`): a key that may list everyone's proposals is a staff key and is refused, and a sample of the newest records it reads must each open for a visitor on the public site. That is a spot check; the full check is `tests/no_leak.py` with the catalogue's database, which the client can run. The same key check guards the demo catalogues page.

## Usage figures

The signed-in service serves a staff page at `/usage`: calls per day, per tool, per service, per outcome and per key over the last 30 days, and what became of proposals (pending, accepted, rejected, by kind). It opens for a catalogue key that may list everyone's proposals (administrators and editors); the key is used for that page only. Keys appear as the first 8 characters of a hash. Point `GLAM_USAGE_LOGS` at both services' logs, and route `/usage` to the signed-in service in nginx (see `docs/nginx-mcp.theahg.co.za.conf`).

## Tests

`tests/test_server.py` and `tests/test_auth.py` run against a fake catalogue, with no network: every read tool's answers and refusals (401, 403, 404, 429, 501, 500), catalogue choice and keys, image fetching (own host only, no redirects, image types only, size limit), stock AtoM field filtering, the hourly proposal limit, the key check, log contents, and sign-in (hashed tokens, encrypted keys, single-use codes, rotation, revocation, a key revoked at the catalogue). CI runs them on every push:

    docker run --rm -v "$PWD/tests:/app/tests:ro" ahg/glam-mcp:phase2 python -m unittest discover -s tests -p 'test_*.py'

`tests/no_leak.py` checks that hidden records (drafts and, with the AHG extensions, embargoed, classified and ICIP-restricted records) never come back through any tool: not by reference, not in search, not among children, not named in a visible record's hierarchy. On every push, GitHub Actions starts a throwaway stock AtoM 2.10.2 with published and draft records (`tests/stock-atom`), runs the public service against it and runs the test (`.github/workflows/no-leak.yml`). To run it by hand:

    cd tests/stock-atom && docker compose -p stock-atom up -d --build
    docker compose -p stock-atom exec -T atom sh /opt/setup.sh    # prints READER_API_KEY and ADMIN_API_KEY
    # start the server with CATALOGUE_URL=http://127.0.0.1:8097 and the reader key, then:
    GLAM_MCP_URL=http://127.0.0.1:8000/mcp CATALOGUE_DB_CNF=db.cnf CATALOGUE_DB=atom PUBLIC_CONTROL_SLUG=published-letter python tests/no_leak.py

`docker compose -p stock-atom down -v` removes the test catalogue.

## Versions

- **v0.5.0** (26 Oct 2026): security and reliability release after an independent source review. Image fetching bound to the catalogue's own host; service-key check at start-up; stock AtoM fields only as the public page shows them; logs without free text, rotated and deleted after 90 days; hourly proposal limit kept on disk; unit tests (fake catalogue) in CI.
- **v0.4.0** (26 Oct 2026): several catalogues (`list_catalogues`, `catalogue` on every read tool, search across all), demo catalogues page, staff usage page, hosted clients (`hosting/client.sh`), ready-made image, key check.

## Licence

Copyright (C) 2026 Johan Pieterse, Plain Sailing Information Systems. Released under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later); see `LICENSE`. Anyone who runs a changed version as a network service must offer its users the changed source.

## Contact

The Archive and Heritage Digital Commons Group - support@theahg.co.za
