# AHG Archives MCP server

An MCP server for AtoM and Heratio catalogues. It lets an assistant work with a catalogue the way an archivist would: find records, read them with their children, images, text and provenance, and propose improvements. Nothing it proposes changes a record until an archivist reviews and approves it.

In development. See it in action on AtoM: [AtoM MCP Server in Action](https://youtu.be/n6snV-lgH_Y).

## Try it

- **Without a key:** connect any MCP client to `https://mcp.theahg.co.za/public/mcp`. Read-only, published records of the PSIS demonstration catalogue.
- **With a personal read-only key:** for testing on your own agent, ask support@theahg.co.za for a key (name, organisation, what you want to try, for how long). It reads what a visitor sees, has an end date and a daily limit, and works with `https://mcp.theahg.co.za/mcp`. Suggesting needs a staff key.
- Every key has an hourly limit; each person may make at most 200 suggestions an hour. A used-up limit answers in plain words with the time it resets.

The quick-start guide (`docs/AHG_Archives_MCP_Quick_Start.md`) covers connecting browser and desktop clients, good questions, and suggesting improvements.

## Tools

| Tool | What it does |
| --- | --- |
| `search_records` | Search descriptions by text, optionally under one parent. |
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

What a caller sees follows AtoM's own permissions: unpublished records, hidden children and master files stay hidden from callers who could not see them in AtoM. `tests/no_leak.py` checks this against a live catalogue.

## Backends

The server finds out which of these a catalogue has when it starts (`GLAM_BACKEND=auto`), so the same install serves both.

- **`atom-ahg`**: AtoM with the AHG Extensions, through REST API v2 (`ahgAPIPlugin`). Needs atom-ahg-plugins v3.121.63 or later for `propose_transcription` and `propose_translation`, v3.121.62 for `propose_relationships`, v3.121.61 for `list_my_proposals`, v3.121.60 for `search_authorities` and `existing_id`, v3.121.45 for authority details on proposals (v3.121.43 for the rest). Proposals are queued in AI > NER review and AI > Suggest review for an archivist; with the Authority Resolution plugin they also reach its queue.
- **Heratio** (planned, heratio#1558): Heratio's REST API v2 has the same shape. Once it gains the proposals and record-text endpoints and returns provenance in the full record, the `atom-ahg` backend serves Heratio too.
- **`atom-base`**: stock AtoM, through its own REST API (`arRestApiPlugin`), tested on AtoM 2.10.2. Search, records, children and `work_through_series` work; authority records, IIIF manifests and file text are not offered by the stock API and say so. Proposals are not saved: the stock write API overwrites rather than adds. Instead, names and places come back as a one-row CSV for AtoM's own **Import > CSV** (type *Archival description*, update behaviour *Update matches ignoring blank fields in CSV*, tick *Skip unmatched records*), which adds them to the record and leaves the rest as it is. Dates are listed for entry by hand, because an import replaces the record's dates. AtoM matches the file on the record's identifier, title and repository, so a record without a repository cannot be updated this way.

### Stock AtoM: setting up

1. Admin > Plugins: enable **arRestApiPlugin**.
2. Create a user with **no groups** for the public service and generate its REST API key (edit the user). Signed-in archivists use their own keys.
3. Set `CATALOGUE_URL`, `CATALOGUE_API_KEY` and `CATALOGUE_NAME`; `GLAM_BACKEND=auto` finds the stock API.

AtoM 2.10.2's REST API checks only the "read" permission, not "view draft", so any key can read draft records through it. The server therefore treats a record on stock AtoM as visible only when a visitor can open its public page, which applies the catalogue's own rules; anything else is "not found". Keep the API key away from anything but this server.

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
4. Build and start: `docker build -t ahg/glam-mcp:phase2 . && docker compose up -d`, then check `curl 127.0.0.1:8031/healthz`.
5. Put the two endpoints behind HTTPS on your own name. `docs/nginx-mcp.theahg.co.za.conf` is a working example to copy and rename.
6. Run `tests/no_leak.py` against your catalogue before opening it to anyone.

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
| `GLAM_DATA` | No | Where sign-in tokens are kept (hashed, keys encrypted). |
| `GLAM_CALL_LOG` | No | Optional JSON-lines log of tool calls. |
| `GLAM_PROPOSAL_CAP` | No | Proposal items one signed-in person may queue per hour (default 200). |
| `GLAM_USAGE_LOGS` | No | Signed-in service only: comma-separated call logs for the staff usage page at `/usage` (default: its own log). Add the public service's log to see both. |

The older names (`PSIS_PUBLIC_BASE`, `PSIS_API_BASE`, `PSIS_API_KEY`, `GLAM_SOURCE_LABEL`, `GLAM_PUBLIC_BASE`, `GLAM_SECRET`) are still accepted.

`docs/AHG_Archives_MCP_Quick_Start.md` is the guide for archivists using AHG's hosted service: connecting, checking it works, asking good questions and reviewing proposals.

## Usage figures

The signed-in service serves a staff page at `/usage`: calls per day, per tool, per service, per outcome and per key over the last 30 days, and what became of proposals (pending, accepted, rejected, by kind). It opens for a catalogue key that may list everyone's proposals (administrators and editors); the key is used for that page only. Keys appear as the first 8 characters of a hash. Point `GLAM_USAGE_LOGS` at both services' logs, and route `/usage` to the signed-in service in nginx (see `docs/nginx-mcp.theahg.co.za.conf`).

## Tests

`tests/no_leak.py` checks that hidden records (drafts and, with the AHG extensions, embargoed, classified and ICIP-restricted records) never come back through any tool: not by reference, not in search, not among children, not named in a visible record's hierarchy. On every push, GitHub Actions starts a throwaway stock AtoM 2.10.2 with published and draft records (`tests/stock-atom`), runs the public service against it and runs the test (`.github/workflows/no-leak.yml`). To run it by hand:

    cd tests/stock-atom && docker compose -p stock-atom up -d --build
    docker compose -p stock-atom exec -T atom sh /opt/setup.sh    # prints READER_API_KEY and ADMIN_API_KEY
    # start the server with CATALOGUE_URL=http://127.0.0.1:8097 and the reader key, then:
    GLAM_MCP_URL=http://127.0.0.1:8000/mcp CATALOGUE_DB_CNF=db.cnf CATALOGUE_DB=atom PUBLIC_CONTROL_SLUG=published-letter python tests/no_leak.py

`docker compose -p stock-atom down -v` removes the test catalogue.

## Licence

Copyright (C) 2026 Johan Pieterse, Plain Sailing Information Systems. Released under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later); see `LICENSE`. Anyone who runs a changed version as a network service must offer its users the changed source.

## Contact

The Archive and Heritage Digital Commons Group - support@theahg.co.za
