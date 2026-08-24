# sunami-grok-search

Fork of [GuDaStudio/GrokSearch](https://github.com/GuDaStudio/GrokSearch)
(branch `grok-with-tavily`, MIT). Upstream's design — Grok finds the sources,
Tavily fetches them, Claude reads them — is kept intact. This fork replaces the
one piece that does not work when you point the server at the official xAI API
instead of a search-capable gateway.

## The problem this fork fixes

Upstream's `web_search` posts a bare `/v1/chat/completions` request:

```python
payload = {
    "model": self.model,
    "messages": [{"role": "system", "content": search_prompt},
                 {"role": "user", "content": time_context + query}],
    "stream": True,
}
```

No tool declarations. No `search_parameters`. Retrieval is expected to happen
**server-side, on the upstream endpoint**. That holds for the GuDa gateway. Point
the same code at `https://api.x.ai/v1` and nothing retrieves anything — the model
answers from parametric memory.

It fails loudly in one specific way. Upstream's `search_prompt` demands:

> **Every sentence must cite sources** (`citation_card`). More references =
> stronger credibility. Silence if uncited.

With no retrieval behind it, the model satisfies that instruction by inventing
citations. Observed output, verbatim:

```
citation_card{source="Unity Performance Optimization Handbook, 2026"}
citation_card{source="Internal Unity Profiler telemetry from shipped titles, 2024-2026"}
```

Meanwhile `sources.py` scrapes sources out of the model's prose with regexes —
including a pattern for `citation_card` itself. Fabricated cards carry no URL, so
the scrape yields nothing and every call returns `sources_count: 0`. The answer
looks meticulously sourced and is not sourced at all.

## What changed

Retrieval now goes through xAI's Responses API with the real search tools, and
citations are read from structured API fields instead of scraped from prose.

| | upstream | this fork |
|---|---|---|
| Endpoint | `/v1/chat/completions` | `/v1/responses` |
| Retrieval | delegated to the gateway | `web_search` + `x_search` tools |
| Citations | regex over model prose | `annotations[].url_citation` |
| X / Twitter | only via a prompt hint | first-class tool with filters |
| Requires | a search-capable gateway | any xAI-compatible key |

### New capability: X search

X is the reason to use Grok at all — live community reaction and developer
chatter that no web index carries yet. It is now a real tool with real filters,
exposed as `web_search` parameters:

- `x_handles` / `exclude_x_handles` — comma-separated, max 20 each, `@` optional
- `from_date` / `to_date` — ISO8601. A hard bound on X, only a recency
  preference on web (measured — see below)
- `allowed_domains` / `excluded_domains` — web search only

Setting `x_handles` narrows the request to X only. Leaving the web tool on
alongside a handle filter drowns the X results: a live run with
`x_handles="unity"` and the deliberately vague query *"recent posts about the
engine"* returned 8 sources, of which only 2 were from `@unity` — the rest were
an Instagram reel, a CNBC story about **aircraft** engines, and
unrealengine.com. With the narrowing in place the same query returns 4 sources,
all four `x.com/unity/status/...`, with no web leakage. Pass `allowed_domains`
or `excluded_domains` if you genuinely want web results alongside the handle
filter.

### Search modes

`GROK_SEARCH_MODE`, or the per-call `mode` parameter:

- `native` — Responses API tools only. Failures are **reported**, never silently
  downgraded; a user who asked for X search must not be handed a memory dump.
- `legacy` — upstream's prompt-only path. For gateways that do their own
  retrieval (GuDa, OpenRouter `:online`).
- `auto` *(default)* — try native, fall back to legacy when the endpoint rejects
  it (400/404/405/422/501). Transient failures (429, 5xx) go to the retry path
  instead, so rate limits don't silently degrade you to the worse mode.

### Honest zero

`sources_count` now means what it says. `0` means retrieval genuinely returned
nothing, and every factual claim in `content` should be treated as unverified.
The replacement system prompt (`native_search_prompt`) forbids the model from
writing citation-shaped text of its own and requires it to say
`No verifiable source found for: <claim>` instead of filling the gap.

The response also carries `search_mode`, so callers can see which path actually
served the request rather than guessing.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROK_API_URL` | — | `https://api.x.ai/v1` for the official API |
| `GROK_API_KEY` | — | xAI key |
| `GROK_MODEL` | `grok-4.6` | Must support the search tools |
| `GROK_SEARCH_MODE` | `auto` | `native` / `legacy` / `auto` |
| `GROK_X_SEARCH` | `true` | Enable the `x_search` tool |
| `GROK_WEB_SEARCH` | `true` | Enable the `web_search` tool |
| `TAVILY_API_KEY` | — | Powers `web_fetch` / `web_map` (unchanged) |

The default model moved from upstream's `grok-4.20-beta` to `grok-4.6`.
`grok-4.20-beta` is not in the `/models` listing — it resolves through an alias
that can disappear — and `grok-4.6` is the model xAI documents as supporting
`x_search`.

## Install

```bash
claude mcp remove grok-search
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": ["--from", "git+https://github.com/zhehaosun717/sunami-grok-search", "grok-search"],
  "env": {
    "GROK_API_URL": "https://api.x.ai/v1",
    "GROK_API_KEY": "xai-...",
    "TAVILY_API_KEY": "tvly-..."
  }
}'
```

## Tests

```bash
uv venv && uv pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ -q
```

45 offline tests, no API key required. They cover citation extraction, malformed
payload degradation, tool-payload construction, handle capping, the
fallback/no-fallback status split, and all three search modes end to end. The
regression case worth knowing about is
`test_hallucinated_citation_card_yields_no_sources`: prose full of
`citation_card` text must still report zero sources.

## Verified against the live API

A run against `api.x.ai` with `tools: [{"type": "x_search"}]` confirms the
design end to end:

- The response contains `custom_tool_call` items — retrieval genuinely runs.
- X posts come back as ordinary `url_citation` annotations, same shape as web
  results. The docs do not state this; it had to be measured.
- `sources_from_responses_payload` parsed 8 annotations into 7 unique sources.
- A full `web_search` call returned `search_mode: native`, `sources_count: 4`,
  with live URLs including a `discussions.unity.com` thread reporting Unity 6
  physics being slower than 2022.3 LTS.

### Date filtering on X: measured, strictly enforced

An early run suggested `from_date` was being ignored — a call with
`from_date="2026-07-23"` cited an October 2024 review. A controlled experiment
shows that was the web tool, not X.

X status IDs are snowflakes: `timestamp_ms = (id >> 22) + 1288834974657`. That
makes every returned post objectively datable straight from its URL, with no
reliance on what the model claims. Three cells, same query, same
`allowed_x_handles=["unity"]`, `x_search` only, varying just the dates:

| cell | params | returned range | out of range |
|---|---|---|---|
| A | none | 2026-07-21 .. 2026-08-19 | — |
| B | `from_date=2026-08-01` | 2026-08-13 .. 2026-08-21 | 0 / 6 |
| C | `from_date=2024-01-01`, `to_date=2024-12-31` | 2024-01-31 .. 2024-07-22 | 0 / 3 |

Cell C is the informative one: it surfaced posts from January, May and July
2024 — content that never appears in the unfiltered baseline. The dates steer
retrieval rather than post-filtering a recent result set, so they are usable as
a real recency guarantee on X.

Reproduce with `scripts/exp_dates.py`.

### Date filtering on the web: a hint, not a bound

The web tool behaves differently, and the difference matters when you rely on
recency. Same query, `web_search` only, with and without `from_date`:

| cell | params | sources | overlap with baseline |
|---|---|---|---|
| W1 | none | 6 | — |
| W2 | `from_date=2026-08-01` | 8 | 3 |

`from_date` clearly *influences* web retrieval — only half the baseline URLs
survived and the rest of the set changed. But it does not bound it: W2 cited
`unity.com/blog/unity-6-features-announcement`, the Unity 6 launch announcement,
whose content predates the cutoff by well over a year.

Treat it as a recency preference on web and a hard constraint on X.

One caveat on the strength of this claim: a single counterexample disproves
strict enforcement, but "content date" is not the same as "crawl or last-updated
date", and a vendor blog post can be edited long after publication. The
direction of the finding is solid; the mechanism behind it is not established.

Reproduce with `scripts/exp_web_dates.py`.

## Not changed

`web_fetch` and `web_map` still go through Tavily (with Firecrawl fallback), and
they were already the strongest part of upstream — Tavily pulls pages that a
plain fetcher gets a 403 on. The planning tools (`plan_intent` and friends),
`switch_model`, and `toggle_builtin_tools` are untouched.

One upstream recommendation is worth ignoring: `toggle_builtin_tools` disables
Claude Code's own WebSearch/WebFetch to force traffic through this server. Do not
run it until `native` mode is confirmed working against your key — otherwise it
removes the only retrieval path that functions.
