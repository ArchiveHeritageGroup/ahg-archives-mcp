# Release evidence

What has been shown, and how, for each release. The external review's "10/10" list (26 October 2026) is the checklist; its item numbers are given.

## Automated, on every push (GitHub Actions)

Each run builds the image and runs, in it: the unit tests (fake catalogue, no network); a throwaway stock AtoM 2.10.2 with published and draft records; the leak test (`tests/no_leak.py`) against it; and the leak test again through a second catalogue. A version tag publishes the image only after these pass.

| Release | Commit | CI run | Image run | Unit tests |
| --- | --- | --- | --- | --- |
| v0.5.0 | 7b457dd, 0fb119e | 40270152152, 40270368346 | 40270610809 | 28 |
| v0.5.1 | b5328ef | 40273801657 | 40274097222 | 28 |
| v0.5.2 | c8ed851 | 40275062629 | 40275338238 | 39 |

## Item 20: no suggestion bypasses review

1. **The MCP cannot send a write.** `tests/test_server.py::NoWritePath` reads the code and fails if any call other than `POST /search` and `POST /descriptions/{slug}/proposals` could be sent, or if a client call with a variable method appears outside the one API helper.
2. **The catalogue's proposal endpoint writes only review queues** (ahgAPIPlugin `descriptionProposalsAction`, read 26 Oct 2026): `ahg_ner_extraction`, `ahg_ner_entity`, `ahg_ner_entity_evidence`, `ahg_relationship_proposal`, `ahg_description_suggestion`, and through Authority Resolution `ahg_mention`, `ahg_mention_context`, `ahg_mention_candidate`. It refuses a caller without edit rights on the record (`AclService::check($resource, 'update')`). Records change only when an archivist approves in the review screens.
3. **Live check on PSIS:** `stuff/test-20-no-bypass.sh` fingerprints the record tables (CHECKSUM TABLE), sends every kind of suggestion through the signed-in service, fingerprints again. Result: pending (Dr Johan Pieterse to run).

## Item 1: stock AtoM visibility

Since v0.6.0 no field on stock AtoM comes from its REST API: every value is read from the record's public page, as AtoM rendered it for a visitor. Unit tests prove a value the API returns but the page does not show never comes through, and that the API is not asked for the record. Leak test on stock AtoM 2.10.2: 3 hidden records, 13 checks, passed as home and as a second catalogue.

## Interoperability (items 16 to 18)

- Heratio: atom-ahg backend unchanged, checked by heratio-dev on Heratio dev with all 17 tools; leak test 40 hidden / 201 checks (as shipped) and 60 hidden / 299 checks (with embargo, classification and ICIP fixtures).
- OpenRiC: adapter built, unit tested against its real response shapes; held from live use until OpenRiC filters unpublished records.

## Pending

- Item 5: sign-in through a real browser (`tests/SIGNIN_TEST.md`).
- Item 7: independent security assessment.
- Item 25: pilot with a partner institution.
