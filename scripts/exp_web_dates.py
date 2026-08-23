"""web_search 侧的 from_date 是否生效？

X 侧已证实严格生效。web URL 没有可客观解码的时间戳，所以改用可判别的
标志物：Unity 6 的发布公告页和 2024 年的评测页，内容日期明确早于 2026-08。
若加了 from_date=2026-08-01 之后这些页面仍被引用，就是未生效的证据。
"""

import asyncio
import os

os.environ["GROK_API_URL"] = "https://api.x.ai/v1"
os.environ.setdefault("TAVILY_ENABLED", "false")
assert os.environ.get("GROK_API_KEY"), "GROK_API_KEY not set"

from grok_search.providers.grok import GrokSearchProvider
from grok_search.sources import sources_from_responses_payload

QUERY = "Unity 6 performance: developer complaints and benchmark results"

# 这些 URL 的内容日期明确早于 2026-08（Unity 6 于 2024 年发布）
KNOWN_OLD_MARKERS = (
    "unity.com/blog/unity-6",
    "creativebloq.com/3d/unity-6-review",
    "unity.com/releases/unity-6",
)


def old_markers(urls):
    return [u for u in urls if any(m in u.lower() for m in KNOWN_OLD_MARKERS)]


async def cell(label, **kw):
    p = GrokSearchProvider(
        os.environ["GROK_API_URL"], os.environ["GROK_API_KEY"], "grok-4.6"
    )
    try:
        payload = await p.search_native(QUERY, use_web=True, use_x=False, **kw)
    except Exception as e:
        print("[%s] FAILED: %s %s" % (label, type(e).__name__, e))
        return label, None
    _, sources = sources_from_responses_payload(payload)
    urls = [s["url"] for s in sources]
    print("\n[%s] params=%s" % (label, kw or "none"))
    print("  sources: %d" % len(urls))
    for u in urls:
        mark = " <-- pre-2026-08 content" if old_markers([u]) else ""
        print("    %s%s" % (u, mark))
    return label, urls


async def main():
    _, base = await cell("W1 no-filter")
    _, filt = await cell("W2 from=2026-08-01", from_date="2026-08-01")

    print("\n" + "=" * 64)
    print("VERDICT (web side)")
    if base is None or filt is None:
        print("  inconclusive: a cell failed")
        return

    old_in_filtered = old_markers(filt)
    print("  W1 sources: %d (known-old markers: %d)"
          % (len(base), len(old_markers(base))))
    print("  W2 sources: %d (known-old markers: %d)"
          % (len(filt), len(old_in_filtered)))
    overlap = set(base) & set(filt)
    print("  URL overlap W1/W2: %d" % len(overlap))

    if old_in_filtered:
        print("  -> from_date appears NOT enforced on web_search")
        for u in old_in_filtered:
            print("     leaked:", u)
    elif overlap == set(base) == set(filt):
        print("  -> identical result sets; from_date had no observable effect")
    else:
        print("  -> no known-old markers leaked; consistent with enforcement,")
        print("     but web dates are not directly measurable -- weaker evidence")
        print("     than the X-side result.")


asyncio.run(main())
