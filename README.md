# AtoM MCP server

An MCP server that lets an assistant work with an Access to Memory (AtoM) catalogue the way an archivist would: find records, read them with their children, images, text and provenance, and propose improvements. Nothing it proposes changes a record until an archivist reviews and approves it.

In development. See it in action: [AtoM MCP Server in Action](https://youtu.be/n6snV-lgH_Y).

## Tools

| Tool | What it does |
| --- | --- |
| `search_records` | Search descriptions by text, optionally under one parent. |
| `get_record` | One description in full: dates, creators, access points, hierarchy, children, images and the provenance chain. |
| `get_children` | The records directly below a description. |
| `get_authority` | An authority record (person, family or organisation). |
| `get_iiif_manifest` | The IIIF manifest for a record's images. |
| `get_record_text` | Text held for a record's files: extracted text, OCR and transcriptions, paged. |
| `get_record_image` | The record's reference image, for the assistant to look at. |
| `propose_access_points_and_dates` | Propose people, organisations, places and dates, each with its evidence and sources. People and organisations may carry dates of existence, a history and places for a new authority record. |
| `propose_description_change` | Propose new text for one field of a description (title, scope and content, archival history and others). |

What a caller sees follows AtoM's own permissions: unpublished records, hidden children and master files stay hidden from callers who could not see them in AtoM. `tests/no_leak.py` checks this against a live catalogue.

## Backends

- **`atom-ahg`** (default): AtoM with the AHG Extensions, through REST API v2 (`ahgAPIPlugin`). Needs atom-ahg-plugins v3.121.44 or later for authority details on proposals (v3.121.43 for the rest). Proposals are queued in AI > NER review and AI > Suggest review for an archivist; with the Authority Resolution plugin they also reach its queue.
- **`atom-base`**: stock AtoM, through its own REST API. Read tools work; proposals are returned to the assistant for the archivist to apply by hand, because the stock API cannot add access points without overwriting them.

## Running it

Docker, one image, two services: a read-only public endpoint with no sign-in, and a signed-in endpoint where each person uses their own catalogue key (through OAuth sign-in, so it works with assistants that connect by URL).

```bash
cp docker-compose.example.yml docker-compose.yml   # edit the catalogue addresses
cat > .env <<'EOF'
PSIS_API_KEY=<read-only service user's API key>
GLAM_SECRET=<Fernet key>
EOF
docker build -t ahg/glam-mcp:phase2 .
docker compose up -d
```

Generate `GLAM_SECRET` with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Never commit `.env` or `data/`; both are in `.gitignore`.

| Setting | Meaning |
| --- | --- |
| `GLAM_BACKEND` | `atom-ahg` (default) or `atom-base`. |
| `PSIS_PUBLIC_BASE` | The catalogue's public address, used in links. |
| `PSIS_API_BASE` | The catalogue's API address. |
| `PSIS_API_KEY` | Key of a read-only service user, for the public endpoint. |
| `GLAM_SOURCE_LABEL` | How answers name the catalogue. |
| `GLAM_AUTH` | `off` (public, read-only) or `oauth` (signed in). |
| `GLAM_PUBLIC_BASE` | This server's public address, for OAuth. |
| `GLAM_HTTP_PATH` | Endpoint path, e.g. `/mcp` or `/public/mcp`. |
| `GLAM_SECRET` | Fernet key that encrypts stored catalogue keys. |
| `GLAM_DATA` | Where sign-in tokens are kept (hashed, keys encrypted). |
| `GLAM_CALL_LOG` | Optional JSON-lines log of tool calls. |

`docs/nginx-mcp.theahg.co.za.conf` is a working nginx site for the two endpoints, and `docs/AHG_Archives_MCP_Quick_Start.md` is the guide for archivists: connecting, checking it works, asking good questions and reviewing proposals.

## Contact

The Archive and Heritage Digital Commons Group - support@theahg.co.za
