# Security policy

## Reporting a problem

Please do not open a public issue for a security problem. Write to **support@theahg.co.za** with "AHG Archives MCP security" in the subject, and include what you found, how to reproduce it, and which version or address it concerns. You will get an answer the same working day (weekdays 08:00 to 17:00 SAST) when records or keys may be exposed, and within two working days otherwise.

We will tell you what we are doing about it and when a fix is released, and credit you in the release notes if you wish.

## What counts

Anything that lets a caller see what a visitor of the catalogue may not (drafts, embargoed, classified or restricted records, masters, hidden fields), read or use someone else's key, write to a record without an archivist's approval, or turn the service against other hosts. `README.md` ("Security") describes what the service promises, and `docs/EVIDENCE.md` how each promise is tested.

Problems in a catalogue itself (AtoM, Heratio, OpenRiC) go to that catalogue's maintainers; tell us as well when the MCP passes them on.

## Supported versions

The latest release on `ghcr.io/archiveheritagegroup/ahg-archives-mcp`. Fixes are not back-ported; update to the latest version (see `docs/OPERATIONS.md`).

## What runs on every change

Unit tests against a fake catalogue, the no-leak test against a stock AtoM with hidden records (as home catalogue and as a second one), and a scan of the dependencies for known vulnerabilities, which fails the build; a dependency list (CycloneDX SBOM) is kept with each run.
