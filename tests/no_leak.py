"""No-leak test: hidden PSIS records must never come back through the GLAM MCP.

Hidden = draft, under an active embargo, security-classified, or under an ICIP
restriction. For each one (up to a cap) the test asks the running service, the
way an assistant would:

  - get_record, get_iiif_manifest and get_record_text must answer "not found";
  - a search for its exact title must not return it;
  - get_children of its parent must not list it;
  - no visible descendant may name it in its hierarchy ("part_of").

Plus a positive control that a known public record is found, so a dead service
cannot pass. Reads the hidden list from the database (read only).

    GLAM_MCP_URL=http://127.0.0.1:8031/mcp python tests/no_leak.py
"""

import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("GLAM_MCP_URL", "http://127.0.0.1:8031/mcp")
DB_CNF = os.environ.get("PSIS_DB_CNF", "")  # mysql --defaults-extra-file
PUBLIC_CONTROL = os.environ.get("PUBLIC_CONTROL_SLUG", "hendrik-and-debora-engelbrecht")
CAP = int(os.environ.get("NO_LEAK_CAP", "40"))

HIDDEN_SQL = """
SELECT DISTINCT io.id, s.slug, COALESCE(i.title, ''), io.parent_id, h.why FROM (
  SELECT object_id id, 'draft' why FROM status WHERE type_id = 158 AND status_id = 159
  UNION SELECT object_id, 'embargo' FROM rights_embargo
        WHERE lifted_at IS NULL AND (end_date IS NULL OR end_date > NOW()) AND (start_date IS NULL OR start_date <= NOW())
  UNION SELECT object_id, 'classified' FROM object_security_classification WHERE active = 1
  UNION SELECT information_object_id, 'icip' FROM icip_access_restriction
        WHERE (end_date IS NULL OR end_date > NOW())
) h
JOIN information_object io ON io.id = h.id
JOIN slug s ON s.object_id = io.id
LEFT JOIN information_object_i18n i ON i.id = io.id AND i.culture = 'en'
WHERE io.id > 1
"""

VISIBLE_CHILD_SQL = """
SELECT s.slug FROM information_object c JOIN slug s ON s.object_id = c.id
JOIN status st ON st.object_id = c.id AND st.type_id = 158 AND st.status_id = 160
WHERE c.lft > (SELECT lft FROM information_object WHERE id = {id})
  AND c.rgt < (SELECT rgt FROM information_object WHERE id = {id}) LIMIT 2
"""


def sql(q: str) -> list[list[str]]:
    out = subprocess.run(["mysql", f"--defaults-extra-file={DB_CNF}", "archive", "-N", "-B", "-e", q],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


async def call(s: ClientSession, tool: str, **args) -> dict:
    r = await s.call_tool(tool, args)
    return json.loads(r.content[0].text) if r.content else {}


async def main() -> int:
    hidden = sql(HIDDEN_SQL)[:CAP]
    slugs_by_id = {h[0]: h for h in hidden}
    failures: list[str] = []
    checks = 0

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()

            control = await call(s, "get_record", slug=PUBLIC_CONTROL)
            if not control.get("found"):
                print(f"FAIL control: public record {PUBLIC_CONTROL} not found - service or key is broken")
                return 1
            print(f"ok   control: {PUBLIC_CONTROL} is found")

            for io_id, slug, title, parent_id, why in hidden:
                label = f"{why} {slug}"
                for tool in ("get_record", "get_iiif_manifest", "get_record_text"):
                    checks += 1
                    if (await call(s, tool, slug=slug)).get("found"):
                        failures.append(f"{label}: {tool} returned it")
                if title.strip():
                    checks += 1
                    hits = (await call(s, "search_records", query=f'"{title[:80]}"', limit=25)).get("results", [])
                    if any(h.get("slug") == slug for h in hits):
                        failures.append(f"{label}: search for its title returned it")
                if parent_id and parent_id != "1" and parent_id not in slugs_by_id:
                    parent_slug = sql(f"SELECT slug FROM slug WHERE object_id = {int(parent_id)}")
                    if parent_slug:
                        checks += 1
                        kids = (await call(s, "get_children", slug=parent_slug[0][0])).get("results", [])
                        if any(k.get("slug") == slug for k in kids):
                            failures.append(f"{label}: listed among the children of {parent_slug[0][0]}")
                for (child,) in sql(VISIBLE_CHILD_SQL.format(id=int(io_id))):
                    checks += 1
                    rec = await call(s, "get_record", slug=child)
                    if any(slug in (a.get("url") or "") or (title and a.get("title") == title) for a in rec.get("part_of", [])):
                        failures.append(f"{label}: named in the hierarchy of visible record {child}")

    print(f"{len(hidden)} hidden records, {checks} checks")
    for f in failures:
        print("FAIL", f)
    print("all passed" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if not DB_CNF:
        sys.exit("Set PSIS_DB_CNF to a mysql defaults file (read-only use)")
    sys.exit(asyncio.run(main()))
