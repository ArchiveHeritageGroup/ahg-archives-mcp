# AHG Archives MCP server

An MCP server for AtoM and Heratio catalogues. It lets an assistant work with a catalogue the way an archivist would: find records, read them with their children, images, text and provenance, and propose improvements. Nothing it proposes changes a record until an archivist reviews and approves it.

In development. See it in action on AtoM: [AtoM MCP Server in Action](https://youtu.be/n6snV-lgH_Y).

## Tools

| Tool | What it does |
| --- | --- |
| `search_records` | Search descriptions by text, optionally under one parent. |
| `get_record` | One description in full: dates, creators, access points, hierarchy, children, images and the provenance chain. |
| `get_children` | The records directly below a description. |
| `get_authority` | An authority record (person, family or organisation). |
| `search_authorities` | Find existing people, organisations, families or places by name, with their ids. |
| `get_iiif_manifest` | The IIIF manifest for a record's images. |
| `get_record_text` | Text held for a record's files: extracted text, OCR and transcriptions, paged. |
| `get_record_image` | The record's reference image, for the assistant to look at. |
| `list_my_proposals` | Your proposals and what became of them (pending, approved, linked, rejected...), so rejected ones are not repeated. Signed-in only. |
| `propose_access_points_and_dates` | Propose people, organisations, places and dates, each with its evidence and sources. People and organisations may carry dates of existence, a history and places for a new authority record. A name or place may point at an existing record (`existing_id`), which the archivist sees as the first candidate to link. |
| `propose_description_change` | Propose new text for one field of a description (title, scope and content, archival history and others). |

What a caller sees follows AtoM's own permissions: unpublished records, hidden children and master files stay hidden from callers who could not see them in AtoM. `tests/no_leak.py` checks this against a live catalogue.

## Backends

- **`atom-ahg`** (default): AtoM with the AHG Extensions, through REST API v2 (`ahgAPIPlugin`). Needs atom-ahg-plugins v3.121.61 or later for `list_my_proposals`, v3.121.60 for `search_authorities` and `existing_id`, v3.121.45 for authority details on proposals (v3.121.43 for the rest). Proposals are queued in AI > NER review and AI > Suggest review for an archivist; with the Authority Resolution plugin they also reach its queue.
- **Heratio** (planned, heratio#1558): Heratio's REST API v2 has the same shape. Once it gains the proposals and record-text endpoints and returns provenance in the full record, the `atom-ahg` backend serves Heratio too.
- **`atom-base`**: stock AtoM, through its own REST API. Read tools work; proposals are returned to the assistant for the archivist to apply by hand, because the stock API cannot add access points without overwriting them.

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
| `GLAM_BACKEND` | No | `atom-ahg` (default) or `atom-base`. |
| `GLAM_AUTH` | No | `off` (public, read-only, the default) or `oauth` (signed in). |
| `MCP_PUBLIC_URL` | With `oauth` | This server's own public address, used for sign-in. |
| `MCP_SECRET` | With `oauth` | Fernet key that encrypts stored catalogue keys. |
| `MCP_SIGNED_IN_URL` | No | Where the public endpoint tells people to sign in to propose changes. |
| `GLAM_HTTP_PATH` | No | Endpoint path, e.g. `/mcp` or `/public/mcp`. |
| `GLAM_DATA` | No | Where sign-in tokens are kept (hashed, keys encrypted). |
| `GLAM_CALL_LOG` | No | Optional JSON-lines log of tool calls. |

The older names (`PSIS_PUBLIC_BASE`, `PSIS_API_BASE`, `PSIS_API_KEY`, `GLAM_SOURCE_LABEL`, `GLAM_PUBLIC_BASE`, `GLAM_SECRET`) are still accepted.

`docs/AHG_Archives_MCP_Quick_Start.md` is the guide for archivists using AHG's hosted service: connecting, checking it works, asking good questions and reviewing proposals.

## Licence

Copyright (C) 2026 Johan Pieterse, Plain Sailing Information Systems. Released under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later); see `LICENSE`. Anyone who runs a changed version as a network service must offer its users the changed source.

## Contact

The Archive and Heritage Digital Commons Group - support@theahg.co.za
