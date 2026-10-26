# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load test: N clients at once, each running a mix of read calls, against a
running service. Reports calls per second and response times (median, 95th
percentile, worst) per tool, and errors. Point it at a test catalogue, never a
client's live one.

    GLAM_MCP_URL=http://127.0.0.1:8000/mcp LOAD_CLIENTS=20 LOAD_ROUNDS=10 \\
      LOAD_SLUG=published-letter python tests/load_test.py
"""

import asyncio
import json
import os
import statistics
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("GLAM_MCP_URL", "http://127.0.0.1:8000/mcp")
CLIENTS = int(os.environ.get("LOAD_CLIENTS", "10"))
ROUNDS = int(os.environ.get("LOAD_ROUNDS", "10"))
SLUG = os.environ.get("LOAD_SLUG", "published-letter")
MIX = [("search_records", {"query": "letter", "limit": 10}), ("get_record", {"slug": SLUG}),
       ("get_children", {"slug": SLUG}), ("list_catalogues", {})]

times: dict[str, list[float]] = {name: [] for name, _ in MIX}
errors: list[str] = []


async def client() -> None:
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for _ in range(ROUNDS):
                for name, args in MIX:
                    t = time.perf_counter()
                    try:
                        res = await s.call_tool(name, args)
                        body = json.loads(res.content[0].text)
                        if body.get("error") in ("unavailable",):
                            errors.append(f"{name}: {body['error']}")
                    except Exception as e:  # noqa: BLE001 - counted, not raised
                        errors.append(f"{name}: {type(e).__name__}")
                    times[name].append(time.perf_counter() - t)


async def main() -> None:
    start = time.perf_counter()
    await asyncio.gather(*(client() for _ in range(CLIENTS)))
    took = time.perf_counter() - start
    total = sum(len(v) for v in times.values())
    print(f"{CLIENTS} clients x {ROUNDS} rounds x {len(MIX)} tools = {total} calls in {took:.1f} s ({total / took:.1f} calls/s), {len(errors)} errors")
    for name, v in times.items():
        v.sort()
        print(f"  {name:<16} median {statistics.median(v) * 1000:6.0f} ms   p95 {v[int(len(v) * 0.95) - 1] * 1000:6.0f} ms   worst {v[-1] * 1000:6.0f} ms")
    for e in sorted(set(errors))[:5]:
        print("  error:", e, errors.count(e))


asyncio.run(main())
