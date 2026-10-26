# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Copyright (C) 2026 Johan Pieterse
# Plain Sailing Information Systems
# Email: johan@plainsailingisystems.co.za
#
# This file is part of the AHG Archives MCP server.
#
# The AHG Archives MCP server is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the License,
# or (at your option) any later version.
#
# It is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with it. If not, see <https://www.gnu.org/licenses/>.

"""No-leak test: hidden records must never come back through the GLAM MCP.

Hidden = draft and, on catalogues with the AHG extensions, under an active
embargo, security-classified, or under an ICIP restriction. Works on PSIS and on
stock AtoM (tests/stock-atom, which CI starts on every push). For each one (up to a cap) the test asks the running service, the
way an assistant would:

  - get_record, get_iiif_manifest and get_record_text must answer "not found";
  - a search for its exact title must not return it;
  - get_children of its parent must not list it;
  - no visible descendant may name it in its hierarchy ("part_of").

Plus a positive control that a known public record is found, so a dead service
cannot pass. Reads the hidden list from the database (read only).

    GLAM_MCP_URL=http://127.0.0.1:8031/mcp CATALOGUE_DB_CNF=~/psis.cnf python tests/no_leak.py

CATALOGUE_DB_CNF (or PSIS_DB_CNF) is a mysql defaults file; CATALOGUE_DB the
database (default archive).
"""

import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("GLAM_MCP_URL", "http://127.0.0.1:8031/mcp")
DB_CNF = os.environ.get("CATALOGUE_DB_CNF") or os.environ.get("PSIS_DB_CNF", "")  # mysql --defaults-extra-file
DB_NAME = os.environ.get("CATALOGUE_DB", "archive")
PUBLIC_CONTROL = os.environ.get("PUBLIC_CONTROL_SLUG", "hendrik-and-debora-engelbrecht")
CAP = int(os.environ.get("NO_LEAK_CAP", "40"))

# Each kind of hidden record, with the table (or table.column) it needs; kinds
# the catalogue lacks (stock AtoM has drafts only) are left out.
HIDDEN_KINDS = [
    ("status", "SELECT object_id id, 'draft' why FROM status WHERE type_id = 158 AND status_id = 159"),
    ("rights_embargo", """SELECT object_id id, 'embargo' why FROM rights_embargo
        WHERE lifted_at IS NULL AND (end_date IS NULL OR end_date > NOW()) AND (start_date IS NULL OR start_date <= NOW())"""),
    # Heratio's embargo table (also present on PSIS); the rule of Heratio's DisclosureGate::embargoedIds.
    ("embargo", """SELECT object_id id, 'embargo' why FROM embargo
        WHERE status = 'active' AND start_date <= CURDATE() AND (end_date IS NULL OR end_date >= CURDATE())"""),
    # Only levels above PUBLIC (level 0) hide a record.
    ("object_security_classification", """SELECT osc.object_id id, 'classified' why FROM object_security_classification osc
        JOIN security_classification sc ON sc.id = osc.classification_id
        WHERE osc.active = 1 AND sc.level > 0"""),
    ("icip_access_restriction", """SELECT information_object_id id, 'icip' why FROM icip_access_restriction
        WHERE (end_date IS NULL OR end_date > NOW())"""),
    # Restrictions that pass down to everything below (Heratio: inherit_to_children, applies_to_descendants).
    ("object_security_classification.inherit_to_children", """SELECT c.id id, 'classified (inherited)' why
        FROM object_security_classification osc JOIN security_classification sc ON sc.id = osc.classification_id
        JOIN information_object p ON p.id = osc.object_id JOIN information_object c ON c.lft > p.lft AND c.rgt < p.rgt
        WHERE osc.active = 1 AND sc.level > 0 AND osc.inherit_to_children = 1"""),
    ("icip_access_restriction.applies_to_descendants", """SELECT c.id id, 'icip (inherited)' why
        FROM icip_access_restriction r JOIN information_object p ON p.id = r.information_object_id
        JOIN information_object c ON c.lft > p.lft AND c.rgt < p.rgt
        WHERE r.applies_to_descendants = 1 AND (r.end_date IS NULL OR r.end_date > NOW())"""),
]
HIDDEN_SQL = """
SELECT DISTINCT io.id, s.slug, COALESCE(i.title, ''), io.parent_id, h.why FROM (
  {kinds}
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
    out = subprocess.run(["mysql", f"--defaults-extra-file={DB_CNF}", DB_NAME, "-N", "-B", "-e", q],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


# Set to test a catalogue the service reads besides its home one (GLAM_OTHER_CATALOGUES).
CATALOGUE = os.environ.get("TEST_CATALOGUE", "")


async def call(s: ClientSession, tool: str, **args) -> dict:
    r = await s.call_tool(tool, {**args, "catalogue": CATALOGUE} if CATALOGUE else args)
    return json.loads(r.content[0].text) if r.content else {}


async def main() -> int:
    tables = {t for (t,) in sql("SHOW TABLES")}
    tables |= {f"{t}.{c}" for t, c in sql("SELECT table_name, column_name FROM information_schema.columns "
                                         "WHERE table_schema = DATABASE() AND column_name IN ('inherit_to_children', 'applies_to_descendants')")}
    # Each kind gets its share of the cap, restricted kinds first, so thousands of
    # drafts cannot crowd out the embargoed, classified and ICIP records.
    present = [q for table, q in HIDDEN_KINDS if table in tables]
    present = present[1:] + present[:1]  # drafts last
    hidden, seen = [], set()
    for i, q in enumerate(present):
        share = (CAP - len(hidden)) // (len(present) - i)
        for row in sql(HIDDEN_SQL.format(kinds=q) + f" LIMIT {share + len(seen)}"):
            if row[0] not in seen and share > 0:
                hidden.append(row); seen.add(row[0]); share -= 1
    if not hidden:
        print("FAIL no hidden records to test - the fixture or the query is wrong")
        return 1
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
        sys.exit("Set CATALOGUE_DB_CNF to a mysql defaults file (read-only use)")
    sys.exit(asyncio.run(main()))
