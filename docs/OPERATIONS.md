# Operations

Running the AHG Archives MCP server: what it keeps, how to update it, back it up, restore it, roll it back and watch it. Each procedure below was carried out once on AHG's own install (26 October 2026); `docs/EVIDENCE.md` records the results.

## What the service keeps

| Where | What | Back up? |
| --- | --- | --- |
| `data/oauth.db` | Sign-ins: token hashes, catalogue keys encrypted with `MCP_SECRET` | Yes |
| `data/proposal-cap.db` | The hourly proposal limit | Yes (losing it only resets an hour) |
| `data/demo-catalogues.json` | Demo catalogues (lapse after 7 days) | Yes |
| `logs/` | Call logs, kept `GLAM_LOG_DAYS` | No: they expire |
| `.env` | `CATALOGUE_API_KEY`, `MCP_SECRET` | In the password vault, never with the backups |

The service holds no copy of any catalogue's records.

## Update

    docker tag ahg/glam-mcp:phase2 ahg/glam-mcp:previous     # keep the running image for a rollback
    git pull && docker build -t ahg/glam-mcp:phase2 . && docker compose up -d
    curl -s 127.0.0.1:8031/healthz; curl -s 127.0.0.1:8032/healthz   # both "ok" (start-up checks the service key: allow about 30 s)

Or with the ready-made image: set `image: ghcr.io/archiveheritagegroup/ahg-archives-mcp:<version>` in `docker-compose.yml`, then `docker compose pull && docker compose up -d`.

## Back up and restore

    hosting/backup.sh backup .                       # backups/mcp-data-<time>.tar.gz, owner-only; safe while running
    hosting/backup.sh restore backups/<file> .       # stops the services, restores data/, starts them

The SQLite files are copied with SQLite's own backup, so a copy taken while the services run is consistent. Restored sign-ins work only with the install's own `MCP_SECRET`: tried on 26 October 2026, 3 of 3 stored keys decrypted with it and 0 with another secret, so a backup without `.env` gives nothing away.

For a hosted client, run the same in `clients/<client>/`.

## Roll back

To the image kept before the update:

    docker tag ahg/glam-mcp:previous ahg/glam-mcp:phase2 && docker compose up -d

To any published version: set `image: ghcr.io/archiveheritagegroup/ahg-archives-mcp:<version>` and `docker compose up -d`. Versions from 0.4.0 on use the same `data/` (0.5.0 added `proposal-cap.db`; an older version ignores it). If a release changed `data/`, restore the backup taken before the update as well.

## Watch

`hosting/healthcheck.sh`, from cron every 5 minutes, checks each service's `/healthz` and raises a Workbench notification when one stops answering `ok` and again when it recovers, once per change:

    */5 * * * * /home/johanpiet/glam-mcp/hosting/healthcheck.sh

`/healthz` answers `ok` only when the catalogue answers a real read with the service key; otherwise `unreachable`, `key_refused` or `not_working`. For hosted clients, list their addresses in `GLAM_HEALTH_URLS`.

## When something is wrong

| Symptom | Look at |
| --- | --- |
| A service will not start | `docker compose logs`: "CATALOGUE_API_KEY refused" means the service key reads more than a visitor or was revoked; "GLAM_DATA must be a writable folder" means `data/` is missing or not writable |
| `key_refused` | The service key was revoked or lost its read scope at the catalogue |
| `unreachable` | The catalogue or the network; the service recovers by itself when it answers again |
| Callers see "limit is used up" | The catalogue key's hourly or daily limit (catalogue side), or 200 proposals an hour (this service) |
| Usage figures | `/usage` on the signed-in service, with an administrator or editor key |
