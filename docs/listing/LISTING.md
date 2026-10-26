# AHG Archives - directory listing

Text and facts for listing the AHG Archives MCP server in public MCP directories (MCP #11). Nothing here is submitted until AHG decides to publish.

## Short description (one line)

Search and read AtoM archival catalogues, with a citable link for every answer; catalogue staff can propose improvements for an archivist to review.

## Description

AHG Archives connects an AI assistant to an archival catalogue built on AtoM (Access to Memory). The assistant searches descriptions, reads them in full with their place in the hierarchy, authority records, file text and page images, and works through a whole fonds or series a batch at a time. Every answer carries the record's public link, so it can be checked and cited.

Only what a visitor of the catalogue may see comes back: drafts, embargoed, security-classified and culturally restricted records stay hidden, and an automated test checks this on every change.

Catalogue staff who connect with their own key can go further. The assistant proposes access points and dates (linked to existing authority records where it finds them), relationships between people, transcriptions of scanned pages and translations, each with its evidence and sources. Nothing changes on a record until an archivist approves it in the catalogue's own review screens.

The same server works with AtoM and the AHG Extensions for AtoM, and with stock AtoM through its own REST API, where proposals come back as a file for AtoM's CSV import. One server can read several catalogues, and the assistant chooses which one, or searches them all at once. Institutions can run it for their own catalogue.

## Facts

| Field | Value |
|---|---|
| Name | AHG Archives |
| Registry name | za.co.theahg/ahg-archives (needs a DNS TXT record on theahg.co.za to publish) |
| Public endpoint (no sign-in, read-only) | https://mcp.theahg.co.za/public/mcp |
| Signed-in endpoint (OAuth or catalogue key) | https://mcp.theahg.co.za/mcp |
| Transport | Streamable HTTP |
| Demonstration catalogue | PSIS, the AHG Extensions for AtoM demonstration site |
| Website | https://www.atom-extensions.com |
| Video | https://youtu.be/n6snV-lgH_Y |
| Licence | AGPL-3.0-or-later |
| Source | ArchiveHeritageGroup/ahg-archives-mcp (private today; most directories expect a public repository for self-hosted servers) |
| Contact | The Archive and Heritage Digital Commons Group - support@theahg.co.za |
| Categories / tags | archives, libraries, museums, cultural heritage, GLAM, AtoM, records management, research |

## Tools

list_catalogues, search_records, get_record, get_children, work_through_series, get_authority, search_authorities, get_iiif_manifest, get_record_text, get_record_image, list_my_proposals, propose_access_points_and_dates, propose_relationships, propose_description_change, propose_transcription, propose_translation.

## Where to list

| Directory | How | Needs |
|---|---|---|
| Official MCP Registry | `mcp-publisher` with server.json from this folder | A DNS TXT record on theahg.co.za (domain namespace) |
| Other public MCP directories | Their submission forms, using the text above | Usually a public repository or a public endpoint (the public endpoint is enough for some) |

Before submitting: make the repository public (stuff/mcp-repo-public.sh; history checked clean 26 Oct 2026), add the DNS TXT record for the registry name, and decide whether the listing waits for the Heratio backend (#9), as the roadmap had it.
