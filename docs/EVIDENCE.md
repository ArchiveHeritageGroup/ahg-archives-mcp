# Release evidence

What has been shown, and how, for each release. The external review's "10/10" list (26 October 2026) is the checklist; its item numbers are given.

## Automated, on every push (GitHub Actions)

Each run builds the image and runs, in it: the unit tests (fake catalogue, no network); a throwaway stock AtoM 2.10.2 with published and draft records; the leak test (`tests/no_leak.py`) against it; and the leak test again through a second catalogue. A version tag publishes the image only after these pass.

| Release | Commit | CI run | Image run | Unit tests |
| --- | --- | --- | --- | --- |
| v0.5.0 | 7b457dd, 0fb119e | 40270152152, 40270368346 | 40270610809 | 28 |
| v0.5.1 | b5328ef | 40273801657 | 40274097222 | 28 |
| v0.5.2 | c8ed851 | 40275062629 | 40275338238 | 39 |
| v0.6.0 | cae4599 | 40276918954 | 40277201522 | 45 |
| v0.6.1 | 228793f | 40280263236 | 40280581094 | 45 |
| v0.7.0 | 6b78635 | 40286950987 | 40287333136 | 52 |
| v0.7.1 | 8dada92 | 40325621922 | 40325748046 | 52 |

## Item 20: no suggestion bypasses review

1. **The MCP cannot send a write.** `tests/test_server.py::NoWritePath` reads the code and fails if any call other than `POST /search` and `POST /descriptions/{slug}/proposals` could be sent, or if a client call with a variable method appears outside the one API helper.
2. **The catalogue's proposal endpoint writes only review queues** (ahgAPIPlugin `descriptionProposalsAction`, read 26 Oct 2026): `ahg_ner_extraction`, `ahg_ner_entity`, `ahg_ner_entity_evidence`, `ahg_relationship_proposal`, `ahg_description_suggestion`, and through Authority Resolution `ahg_mention`, `ahg_mention_context`, `ahg_mention_candidate`. It refuses a caller without edit rights on the record (`AclService::check($resource, 'update')`). Records change only when an archivist approves in the review screens.
3. **Live check on PSIS** (run by Dr Johan Pieterse, 26 October 2026): `stuff/test-20-no-bypass.sh` fingerprinted 21 record tables (CHECKSUM TABLE), sent all five kinds of suggestion through the signed-in service with a staff key, and fingerprinted again.

   | | Before | After |
   | --- | --- | --- |
   | Record tables (21, by checksum) | | unchanged: **PASS** |
   | ahg_ner_extraction | 229 | 230 |
   | ahg_ner_entity | 2169 | 2172 |
   | ahg_ner_entity_evidence | 26 | 29 |
   | ahg_relationship_proposal | 2 | 3 |
   | ahg_description_suggestion | 11 | 14 |
   | ahg_mention / ahg_mention_context | 1399 | 1401 |

   Every suggestion reached its review queue (**PASS**) and no record changed. A run repeating the same text queued nothing, as duplicates are refused; the script now uses new text each run.

## Item 1: stock AtoM visibility

Since v0.6.0 no field on stock AtoM comes from its REST API: every value is read from the record's public page, as AtoM rendered it for a visitor. Unit tests prove a value the API returns but the page does not show never comes through, and that the API is not asked for the record. Leak test on stock AtoM 2.10.2: 3 hidden records, 13 checks, passed as home and as a second catalogue.

## Interoperability (items 16 to 18)

- Heratio: atom-ahg backend unchanged, checked by heratio-dev on Heratio dev with all 17 tools; leak test 40 hidden / 201 checks (as shipped) and 60 hidden / 299 checks (with embargo, classification and ICIP fixtures).
- OpenRiC: adapter built and unit tested against its real response shapes; since OpenRiC v0.17.26 (published only) checked locally against ric.theahg.co.za: search, full record with date ranges, children, and a draft answering not found. Leak test passed (see below).

## Reliability (items 9, 10, 11, 13)

- Unit tests (52): every failure carries an error code (also checked by reading the code), a timeout and a catalogue going down and coming back answer `unavailable` and recover without a restart, and 20 simultaneous proposals against a limit of 5 let exactly 5 through.
- `/healthz` answers `ok` only on a real read with the service key (v0.5.2).
- Load (`tests/load_test.py`, stock AtoM 2.10.2 test catalogue, 26 Oct 2026): before worker threads, 13 calls/s at 25 clients (record read p95 4.4 s); after, 40 calls/s (p95 1.5 s), no errors, memory 48 to 57 MB.

## Operations (items 12, 22, 23)

- Backup of the live install, restored into a scratch folder: both SQLite files pass integrity checks, 3 sign-ins and 6 registered clients as live; 3 of 3 stored keys decrypt with the install's secret, 0 with another.
- Rollback: the published 0.6.0 image starts and answers `ok`.
- Clean install from the public repository by the README alone (fresh clone, ready-made 0.6.1 image, PSIS read-only key): both services healthy, key check ok, search returns PSIS records.
- Health alerts: `hosting/healthcheck.sh` raised one Workbench notification for a dead service, none for a live one, none on repeat.

## Dependencies and disclosure (item 24)

- `pip-audit`: the only findings were in the base image's pip (12 known issues); pip is upgraded in the image, and the scan of everything the service runs reports no known vulnerabilities. CI runs the scan on every push (failing on findings) and keeps a CycloneDX SBOM with the run. `SECURITY.md` describes reporting.

## OpenRiC reference service leak test

26 October 2026, ric.theahg.co.za through a test MCP (openric backend, no key), hidden list from the Heratio database it reads (drafts, embargo, classification, ICIP; read only): 17 hidden records, 73 checks, all passed; control `title-of-object` found. Calls answered "rate limited" are retried and never counted as a pass (OpenRiC allows 60 requests a minute).

## OpenRiC Workspace leak test

26 October 2026, workspace.openric.org through a test MCP (openric backend): the hidden list from Workspace's own database (`tests/openric_workspace_hidden.sql`: deleted, or no published authorised name; 1 of 981 records), 1 hidden record, 4 checks, all passed. Negative control: a published record listed as hidden fails both get_record and search, so the test can fail. Workspace's API takes no key and serves the public view only, so no key can widen it. Not covered: records hidden only by the Public role's permissions.

## Daily cap (plugins #223)

`stuff/test-223-daily-cap.sh` on PSIS, 26 October 2026: a throwaway read-only key with a daily cap of 3 answered 200, 200, 200, then 429 with Retry-After 51172 (seconds to midnight).

## First hosted client

Heratio (26 October 2026): service key checked (reads only what a visitor sees); leak test against heratio.org through the hosted public service, 17 hidden records, 73 checks, all passed; published at https://heratio.mcp.theahg.co.za, checked from outside (health ok, search returns heratio.org records, signed-in address refuses callers without a key).

## Pending

- Item 5: sign-in through a real browser (`tests/SIGNIN_TEST.md`).
- Item 7: independent security assessment.
- Item 25: pilot with a partner institution.
