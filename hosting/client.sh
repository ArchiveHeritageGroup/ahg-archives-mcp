#!/bin/bash
# Hosted AHG Archives MCP for clients' own catalogues: one pair of services per client
# (signed-in and public), its own address, keys, logs, usage and demo pages.
#
#   hosting/client.sh add <client> "<Catalogue name>" <catalogue-url> [host]
#   hosting/client.sh leak-test <client> "<note>"   record that tests/no_leak.py passed against
#                                         the client's catalogue (who ran it, when, how many checks)
#   hosting/client.sh publish <client>    nginx site + certificate (sudo; after DNS and the leak test)
#   hosting/client.sh check <client>      service health and the key check again
#   hosting/client.sh list
#   hosting/client.sh remove <client>     stops it and deletes everything of it: key, secret, data, logs
#
# <client> is a short name (a-z, 0-9, -); the host defaults to <client>.mcp.theahg.co.za.
# The service key is asked for (or taken from CLIENT_KEY): a read-only key from the
# client's catalogue for a user with no groups. It is checked before anything starts.
# GLAM_IMAGE picks the image (default ahg/glam-mcp:phase2, the one built here).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
CLIENTS=$ROOT/clients
IMAGE=${GLAM_IMAGE:-ahg/glam-mcp:phase2}
FIRST_PORT=8041

die() { echo "$*" >&2; exit 1; }
dir_of() { [[ $1 =~ ^[a-z0-9][a-z0-9-]{1,30}$ ]] || die "Client name: 2-31 characters, a-z, 0-9 and -"; echo "$CLIENTS/$1"; }
env_of() { grep "^$2=" "$1/.env" | cut -d= -f2- | sed 's/^"//; s/"$//'; }
healthy() { curl -sf "http://127.0.0.1:$1/healthz" >/dev/null; }

next_port() {
  local max=$((FIRST_PORT - 2)) p
  for f in "$CLIENTS"/*/.env; do
    [ -e "$f" ] || continue
    p=$(grep '^SIGNED_PORT=' "$f" | cut -d= -f2)
    (( p > max )) && max=$p
  done
  echo $((max + 2))
}

case "${1:-}" in
add)
  [ $# -ge 4 ] || die "Usage: $0 add <client> \"<Catalogue name>\" <catalogue-url> [host]"
  D=$(dir_of "$2"); NAME=$3; URL=${4%/}; HOST=${5:-$2.mcp.theahg.co.za}
  [ -e "$D" ] && die "$2 already exists ($D)"
  KEY=${CLIENT_KEY:-}
  [ -n "$KEY" ] || { read -rsp "Read-only API key from $URL (user with no groups): " KEY; echo; }
  [ -n "$KEY" ] || die "No key given."
  echo "Checking the key..."
  docker run --rm --network host "$IMAGE" python -m glam_mcp.keycheck "$URL" "$KEY" || die "Not added."
  SIGNED=$(next_port); PUBLIC=$((SIGNED + 1))
  SECRET=$(docker run --rm "$IMAGE" python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  mkdir -p "$D/logs" "$D/data"
  (umask 077; cat > "$D/.env" <<ENV
CLIENT=$2
HOST=$HOST
SIGNED_PORT=$SIGNED
PUBLIC_PORT=$PUBLIC
GLAM_IMAGE=$IMAGE
RUN_AS=$(id -u):$(id -g)
CATALOGUE_URL=$URL
CATALOGUE_NAME="$NAME"
CATALOGUE_API_KEY=$KEY
MCP_SECRET=$SECRET
ENV
  )
  cp hosting/client-compose.yml "$D/docker-compose.yml"
  docker compose --project-directory "$D" up -d
  for i in $(seq 30); do healthy "$SIGNED" && healthy "$PUBLIC" && break; sleep 2; done
  healthy "$SIGNED" && healthy "$PUBLIC" || { docker compose --project-directory "$D" logs --tail 30; die "Started, but not healthy."; }
  echo "Running for $NAME on 127.0.0.1:$SIGNED (signed-in) and :$PUBLIC (public)."
  echo "Next: DNS for $HOST (CNAME to theahg.ddns.net), then: $0 publish $2"
  ;;
leak-test)
  D=$(dir_of "${2:-}"); [ -e "$D/.env" ] || die "No client $2"
  [ -n "${3:-}" ] || die "Usage: $0 leak-test <client> \"<who ran tests/no_leak.py, when, how many checks passed>\""
  echo "$(date -Is) $3" >> "$D/leak-test.txt"
  echo "Recorded for $2. It may now be published."
  ;;
publish)
  D=$(dir_of "${2:-}"); [ -e "$D/.env" ] || die "No client $2"
  # The key check samples records; only tests/no_leak.py with the catalogue's database
  # proves nothing hidden comes back. A client goes public only after it has passed.
  [ -s "$D/leak-test.txt" ] || die "No leak test recorded for $2. Run tests/no_leak.py against their catalogue (with them), then: $0 leak-test $2 \"<note>\""
  HOST=$(env_of "$D" HOST); SITE=/etc/nginx/sites-available/$HOST.conf
  getent hosts "$HOST" >/dev/null || die "$HOST does not resolve yet. Add the DNS record first."
  sed "s/@HOST@/$HOST/g; s/@SIGNED@/$(env_of "$D" SIGNED_PORT)/g; s/@PUBLIC@/$(env_of "$D" PUBLIC_PORT)/g" hosting/nginx-client.conf | sudo tee "$SITE" >/dev/null
  sudo ln -sf "$SITE" /etc/nginx/sites-enabled/
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d "$HOST" --redirect --non-interactive --agree-tos -m support@theahg.co.za
  curl -sf "https://$HOST/healthz" >/dev/null && echo "Live: https://$HOST/mcp (signed-in) and https://$HOST/public/mcp (public)"
  ;;
check)
  D=$(dir_of "${2:-}"); [ -e "$D/.env" ] || die "No client $2"
  for p in SIGNED_PORT PUBLIC_PORT; do healthy "$(env_of "$D" $p)" && echo "$p ok" || echo "$p NOT healthy"; done
  docker run --rm --network host "$(env_of "$D" GLAM_IMAGE)" python -m glam_mcp.keycheck "$(env_of "$D" CATALOGUE_URL)" "$(env_of "$D" CATALOGUE_API_KEY)"
  ;;
list)
  for f in "$CLIENTS"/*/.env; do
    [ -e "$f" ] || continue
    D=$(dirname "$f")
    printf '%-20s %-36s %s/%s %s\n' "$(env_of "$D" CLIENT)" "$(env_of "$D" HOST)" "$(env_of "$D" SIGNED_PORT)" "$(env_of "$D" PUBLIC_PORT)" \
      "$(healthy "$(env_of "$D" SIGNED_PORT)" && echo up || echo DOWN)"
  done
  ;;
remove)
  D=$(dir_of "${2:-}"); [ -e "$D/.env" ] || die "No client $2"
  HOST=$(env_of "$D" HOST)
  read -rp "Stop $2 ($HOST) and delete its key, secret, data and logs? Type the client name to confirm: " OK
  [ "$OK" = "$2" ] || die "Not removed."
  docker compose --project-directory "$D" down
  if [ -e "/etc/nginx/sites-enabled/$HOST.conf" ]; then
    sudo rm -f "/etc/nginx/sites-enabled/$HOST.conf" "/etc/nginx/sites-available/$HOST.conf"
    sudo nginx -t && sudo systemctl reload nginx
    sudo certbot delete --cert-name "$HOST" --non-interactive || true
  fi
  # Hosted service terms: when an institution leaves, its key, secret, sign-ins,
  # demo catalogues and call logs are all deleted. Back it up first if it is needed.
  rm -rf "$D"
  echo "Removed $2: key, secret, data and logs deleted."
  ;;
*)
  sed -n 2,14p "$0"; exit 1
  ;;
esac
