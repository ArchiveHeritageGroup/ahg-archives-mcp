# SPDX-License-Identifier: AGPL-3.0-or-later
"""After the sign-in test in tests/SIGNIN_TEST.md: what the service recorded for
one test key, from its own call log and sign-in store. Asks for the key; prints
only its fingerprint.

    python3 tests/signin_check.py [logs/calls.jsonl] [data/oauth.db]
"""

import getpass
import glob
import hashlib
import json
import sqlite3
import sys
import time

log_path = sys.argv[1] if len(sys.argv) > 1 else "logs/calls.jsonl"
db_path = sys.argv[2] if len(sys.argv) > 2 else "data/oauth.db"
key = getpass.getpass("Test key (not shown): ").strip()
h = hashlib.sha256(key.encode()).hexdigest()
fp, subject = h[:8], "key:" + h[:12]
print(f"Key fingerprint {fp}")

calls = []
for path in [log_path, *sorted(glob.glob(glob.escape(log_path) + ".*"))]:
    try:
        with open(path, encoding="utf-8") as f:
            calls += [r for r in map(json.loads, f) if r.get("key") == fp]
    except OSError:
        pass
calls.sort(key=lambda r: r["ts"])

db = sqlite3.connect(db_path)
now = time.time()
live = {"access": 0, "refresh": 0}
for kind, data, expires in db.execute("SELECT kind, data, expires FROM tokens WHERE kind IN ('access', 'refresh')"):
    if json.loads(data).get("subject") == subject and (expires is None or expires > now):
        live[kind] += 1

ok = [c for c in calls if c["outcome"] == "ok"]
refused = [c for c in calls if c["outcome"] == "refused"]
checks = [
    ("Signed in and used (an ok call with this key)", bool(ok)),
    ("After revoking, calls were refused", bool(refused) and (not ok or refused[-1]["ts"] >= ok[-1]["ts"])),
]
print(f"Calls with this key: {len(calls)} ({len(ok)} ok, {len(refused)} refused)")
for c in calls[-6:]:
    print(f"  {c['ts']}  {c['tool']:<32} {c['outcome']}")
print(f"Live sign-in tokens for this key: {live['access']} access, {live['refresh']} refresh "
      "(an access token lasts an hour; the catalogue refuses its calls once the key is revoked, "
      "and its refresh is refused)")
for label, passed in checks:
    print(("PASS  " if passed else "FAIL  ") + label)
sys.exit(0 if all(p for _, p in checks) else 1)
