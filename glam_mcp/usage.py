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

"""Usage figures page (/usage, staff only): calls per day, per tool, per service
and per key from the JSON-lines call logs, and what became of proposals.

Staff only, decided by the catalogue: the page opens for a catalogue key that
may list everyone's proposals (administrators and editors). The key is used for
that one request and not kept. ponytail: no session, so the key is entered on
each visit; add one if the page is used daily.
"""

import html
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse

DECIDED_OK = {"approved", "edited", "linked", "new_record_created"}
DECIDED_NO = {"rejected"}

STYLE = """
:root { --gold:#E1B34F; --green:#10373E; --cream:#F2EDE6; --body:#262626; --muted:#C3CDCE; --link:#653332; }
body { font-family: Arial, Helvetica, sans-serif; color: var(--body); background: #fff; margin: 0; }
header { background: var(--green); color: var(--cream); padding: 16px 24px; }
header h1 { color: var(--gold); margin: 0; font-size: 1.5rem; }
main { padding: 16px 24px; max-width: 1100px; }
h2 { color: var(--green); border-bottom: 2px solid var(--gold); padding-bottom: 4px; }
table { border-collapse: collapse; margin: 8px 0 20px; }
th, td { border: 1px solid #999; padding: 4px 10px; text-align: left; }
th { background: var(--green); color: #fff; font-weight: bold; }
td.n { text-align: right; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0 20px; }
.tile { border: 1px solid var(--muted); border-top: 4px solid var(--gold); padding: 10px 16px; min-width: 150px; }
.tile b { display: block; font-size: 1.6rem; color: var(--green); }
.err { color: #8a1c1c; }
a { color: var(--link); }
input { padding: 6px; width: 28em; max-width: 100%; }
button { background: var(--gold); border: 0; padding: 7px 16px; font-weight: bold; cursor: pointer; }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><header><h1>AHG Archives - usage</h1></header><main>{body}</main></body></html>""", status_code=status)


def _form(source: str, message: str = "") -> str:
    return f"""<p>Usage of the AHG Archives service for <strong>{html.escape(source)}</strong>. For catalogue
administrators and editors: enter your catalogue API key (it is used for this page only and not kept).</p>
{f'<p class="err">{html.escape(message)}</p>' if message else ''}
<form method="post"><label>Catalogue API key<br><input type="password" name="key" autocomplete="off" required></label>
<p><button type="submit">Show figures</button></p></form>"""


def _table(head: list[str], rows: list, numeric_from: int = 1) -> str:
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    trs = "".join("<tr>" + "".join(
        f'<td class="n">{v}</td>' if i >= numeric_from and isinstance(v, (int, float, str)) and str(v).replace("%", "").replace(".", "").isdigit()
        else f"<td>{html.escape(str(v))}</td>" for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs or '<tr><td colspan=99>None</td></tr>'}</tbody></table>"


def _read_logs(paths: list[str], since: datetime) -> list[dict]:
    out = []
    for path in paths:
        service = "public" if "public" in path else "signed-in"
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        ts = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S%z")
                    except (ValueError, KeyError):
                        continue
                    if ts >= since:
                        r["_day"], r["_service"] = ts.date().isoformat(), service
                        out.append(r)
        except OSError:
            continue
    return out


def make_usage_route(api_base: str, key_header: str, user_agent: str, source: str, log_paths: list[str], days: int = 30):
    async def usage(request: Request) -> HTMLResponse:
        if request.method == "GET":
            return _page("Usage", _form(source))
        key = str((await request.form()).get("key", "")).strip()
        if not key:
            return _page("Usage", _form(source, "Enter a key."), 400)
        # The catalogue decides: listing everyone's proposals needs an administrator or editor.
        try:
            r = httpx.get(api_base + "/proposals", params={"mine": 0, "count": 200},
                          headers={key_header: key, "User-Agent": user_agent, "Accept": "application/json"}, timeout=20)
        except httpx.HTTPError:
            return _page("Usage", _form(source, "The catalogue could not be reached. Try again later."), 503)
        if r.status_code != 200:
            return _page("Usage", _form(source, "That key may not see usage figures: it needs a catalogue administrator or editor."), 403)
        proposals = (r.json().get("data") or {}).get("results", [])

        since = datetime.now(timezone.utc) - timedelta(days=days)
        calls = _read_logs(log_paths, since)
        per_day, per_tool, per_service, per_outcome, per_key = Counter(), Counter(), Counter(), Counter(), Counter()
        last_use: dict[str, str] = {}
        for c in calls:
            per_day[c["_day"]] += 1
            per_tool[c.get("tool", "?")] += 1
            per_service[c["_service"]] += 1
            per_outcome[(c.get("outcome") or "?").split(":")[0]] += 1
            k = c.get("key") or ("public (no key)" if c.get("caller") == "public" else "own key (before fingerprints)")
            per_key[k] += 1
            last_use[k] = max(last_use.get(k, ""), c["ts"])

        kinds: dict[str, Counter] = defaultdict(Counter)
        for p in proposals:
            kinds[p.get("kind", "?")][p.get("status", "?")] += 1
        prop_rows = []
        for kind, st in sorted(kinds.items()):
            ok, no = sum(st[s] for s in DECIDED_OK), sum(st[s] for s in DECIDED_NO)
            rate = f"{round(100 * ok / (ok + no))}%" if ok + no else "-"
            prop_rows.append([kind, sum(st.values()), st["pending"], ok, no, rate])
        total_ok = sum(r[3] for r in prop_rows)
        total_no = sum(r[4] for r in prop_rows)

        tiles = "".join(f'<div class="tile"><b>{v}</b>{html.escape(label)}</div>' for label, v in [
            (f"tool calls, last {days} days", len(calls)),
            ("days with use", len(per_day)),
            ("keys seen", len([k for k in per_key if len(k) == 8])),
            ("proposals (newest 200)", len(proposals)),
            ("approval rate of decided", f"{round(100 * total_ok / (total_ok + total_no))}%" if total_ok + total_no else "-"),
        ])
        body = f"""<p>Catalogue: <strong>{html.escape(source)}</strong>. Figures from the service's call logs since
{since.date().isoformat()}, and from the catalogue's newest 200 proposals. Keys appear as the first 8 characters of a
hash, never the key itself.</p><div class="tiles">{tiles}</div>
<h2>Calls per day</h2>{_table(["Day", "Calls"], [[d, n] for d, n in sorted(per_day.items(), reverse=True)])}
<h2>Calls per tool</h2>{_table(["Tool", "Calls"], per_tool.most_common())}
<h2>Calls per service</h2>{_table(["Service", "Calls"], per_service.most_common())}
<h2>Outcomes</h2>{_table(["Outcome", "Calls"], per_outcome.most_common())}
<h2>Calls per key</h2>{_table(["Key", "Calls", "Last call"], [[k, n, last_use[k][:16].replace("T", " ")] for k, n in per_key.most_common()])}
<h2>What became of proposals</h2>{_table(["Kind", "Proposals", "Pending", "Accepted", "Rejected", "Accepted of decided"], prop_rows)}
<p>Accepted counts approved, edited, linked and new records created. Rejected proposals are kept so the assistant does not repeat them.</p>"""
        return _page("Usage", body)

    return usage
