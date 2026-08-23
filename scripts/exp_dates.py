"""from_date / to_date 是否真的生效？

方法：X 的 status ID 是 snowflake，高 41 位是毫秒时间戳，
      timestamp_ms = (id >> 22) + 1288834974657
所以可以从返回的 URL 直接解出每条帖子的客观发布时间，
不依赖模型自述，也不依赖网页元数据。

每个单元格固定 handle=@unity、只开 x_search，只变动日期参数。
"""

import asyncio
import os
import re
from datetime import datetime, timezone

os.environ["GROK_API_URL"] = "https://api.x.ai/v1"
os.environ.setdefault("TAVILY_ENABLED", "false")
assert os.environ.get("GROK_API_KEY"), "GROK_API_KEY not set"

from grok_search.providers.grok import GrokSearchProvider
from grok_search.sources import sources_from_responses_payload

TWITTER_EPOCH_MS = 1288834974657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/[^/]+/status/(\d+)")

QUERY = "posts from this account about the game engine"
HANDLES = ["unity"]


def post_time(url: str):
    m = STATUS_RE.search(url)
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def cell(label: str, **date_kwargs):
    p = GrokSearchProvider(
        os.environ["GROK_API_URL"], os.environ["GROK_API_KEY"], "grok-4.6"
    )
    try:
        payload = await p.search_native(
            QUERY,
            use_web=False,
            use_x=True,
            allowed_x_handles=HANDLES,
            **date_kwargs,
        )
    except Exception as e:
        print("[%s] FAILED: %s %s" % (label, type(e).__name__, e))
        body = getattr(getattr(e, "response", None), "text", None)
        if body:
            print("     body:", body[:400])
        return label, None

    _, sources = sources_from_responses_payload(payload)
    dated = []
    undated = []
    for s in sources:
        t = post_time(s["url"])
        (dated if t else undated).append((t, s["url"]))

    dated.sort(key=lambda x: x[0])
    print("\n[%s]  params=%s" % (label, date_kwargs or "none"))
    print("  sources: %d (X posts dated: %d, other: %d)"
          % (len(sources), len(dated), len(undated)))
    for t, u in dated:
        print("    %s  %s" % (t.strftime("%Y-%m-%d"), u))
    for _, u in undated:
        print("    %-10s  %s" % ("(no date)", u))
    if dated:
        print("  range: %s .. %s"
              % (dated[0][0].strftime("%Y-%m-%d"), dated[-1][0].strftime("%Y-%m-%d")))
    return label, dated


async def main():
    cells = [
        ("A no-filter", {}),
        ("B from=2026-08-01", {"from_date": "2026-08-01"}),
        ("C from=2024-01-01 to=2024-12-31",
         {"from_date": "2024-01-01", "to_date": "2024-12-31"}),
    ]
    results = {}
    for label, kw in cells:
        lbl, dated = await cell(label, **kw)
        results[lbl] = dated

    print("\n" + "=" * 64)
    print("VERDICT")

    b = results.get("B from=2026-08-01")
    if b:
        cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
        violations = [(t, u) for t, u in b if t < cutoff]
        print("  B: %d posts, %d before from_date -> from_date %s"
              % (len(b), len(violations),
                 "IGNORED" if violations else "RESPECTED"))
        for t, u in violations[:5]:
            print("     violation:", t.strftime("%Y-%m-%d"), u)
    else:
        print("  B: no dated X posts, inconclusive")

    c = results.get("C from=2024-01-01 to=2024-12-31")
    if c:
        lo = datetime(2024, 1, 1, tzinfo=timezone.utc)
        hi = datetime(2025, 1, 1, tzinfo=timezone.utc)
        outside = [(t, u) for t, u in c if not (lo <= t < hi)]
        print("  C: %d posts, %d outside 2024 window -> window %s"
              % (len(c), len(outside),
                 "IGNORED" if outside else "RESPECTED"))
        for t, u in outside[:5]:
            print("     outside:", t.strftime("%Y-%m-%d"), u)
    else:
        print("  C: no dated X posts, inconclusive")


asyncio.run(main())
