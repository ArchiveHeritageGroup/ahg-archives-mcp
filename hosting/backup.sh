#!/bin/bash
# Back up or restore one AHG Archives MCP install's state: the sign-in store
# (oauth.db: token hashes, encrypted catalogue keys), the hourly proposal limit
# (proposal-cap.db) and demo catalogues (demo-catalogues.json). Logs are not
# included (they expire after GLAM_LOG_DAYS anyway). .env holds the secrets and is
# NOT included: keep it in the password vault; without its MCP_SECRET the backed-up
# catalogue keys cannot be decrypted, which is the point.
#
#   hosting/backup.sh backup  [dir]            dir: the install (default .); writes backups/mcp-data-<time>.tar.gz
#   hosting/backup.sh restore <archive> [dir]  stops the services, restores data/, starts them
#
# SQLite files are copied with ".backup", so a backup taken while the services run is consistent.
set -euo pipefail
cmd=${1:-}; shift || true

case "$cmd" in
backup)
  D=${1:-.}; [ -d "$D/data" ] || { echo "No data/ in $D" >&2; exit 1; }
  W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
  for db in "$D"/data/*.db; do [ -e "$db" ] && sqlite3 "$db" ".backup '$W/$(basename "$db")'"; done
  [ -e "$D/data/demo-catalogues.json" ] && cp "$D/data/demo-catalogues.json" "$W/"
  mkdir -p "$D/backups"
  OUT="$D/backups/mcp-data-$(date +%Y%m%d-%H%M%S).tar.gz"
  (umask 077; tar -czf "$OUT" -C "$W" .)
  echo "Backed up $(ls "$W" | tr '\n' ' ')to $OUT"
  ;;
restore)
  A=${1:?archive}; D=${2:-.}
  tar -tzf "$A" >/dev/null || { echo "Not a readable archive: $A" >&2; exit 1; }
  docker compose --project-directory "$D" stop
  mkdir -p "$D/data"
  tar -xzf "$A" -C "$D/data"
  docker compose --project-directory "$D" start
  echo "Restored $A into $D/data and started the services."
  ;;
*)
  sed -n 2,13p "$0"; exit 1
  ;;
esac
