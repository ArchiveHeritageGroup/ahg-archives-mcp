#!/bin/bash
# Checks each service's /healthz and raises a Workbench notification when one stops
# answering "ok", and again when it recovers - once per change, not on every run.
# For cron, every 5 minutes:
#   */5 * * * * /home/johanpiet/glam-mcp/hosting/healthcheck.sh
# Services to check: GLAM_HEALTH_URLS (space-separated), default the two on this host.
# State is kept in ~/.cache/glam-mcp-health/.
URLS=${GLAM_HEALTH_URLS:-"http://127.0.0.1:8031/healthz http://127.0.0.1:8032/healthz"}
INBOX=${WORKBENCH_NOTIFICATIONS_INBOX:-/var/spool/workbench/notifications}
STATE=${HOME}/.cache/glam-mcp-health
mkdir -p "$STATE"

for url in $URLS; do
  status=$(curl -s -m 20 "$url" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo unreachable)
  [ -n "$status" ] || status=unreachable
  f="$STATE/$(echo "$url" | tr -c 'a-zA-Z0-9' '_')"
  before=$(cat "$f" 2>/dev/null || echo ok)
  echo "$status" > "$f"
  [ "$status" = "$before" ] && continue
  if [ "$status" = ok ]; then title="AHG Archives MCP recovered"; msg="$url answers ok again (was: $before)."
  else title="AHG Archives MCP: $status"; msg="$url answers \"$status\" (was ok). docker compose ps and docker compose logs in ~/glam-mcp."; fi
  [ -d "$INBOX" ] && python3 -c "import json,sys; print(json.dumps({'username':'johan','title':sys.argv[1],'message':sys.argv[2],'eventType':'alert'}))" \
    "$title" "$msg" > "$INBOX/glam-mcp-health-$(date +%s)-$RANDOM.json"
  echo "$title: $msg"
done
