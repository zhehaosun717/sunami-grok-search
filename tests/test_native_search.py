"""sunami 新增路径的单元测试：结构化信源解析 + 工具构造。

这些用例全部离线运行，不需要 API key —— 它们锁住的是上游最脆弱的那一环：
信源到底是从 API 的结构化字段来的，还是从模型正文里刮出来的。
"""

import pytest

from grok_search.providers.grok import GrokSearchProvider, is_endpoint_unsupported
from grok_search.server import _mentions_x
from grok_search.sources import sources_from_responses_payload

import httpx


# --------------------------------------------------------------------------
# sources_from_responses_payload
# --------------------------------------------------------------------------

RESPONSES_PAYLOAD = {
    "output": [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": "Unity 6 的 GC 开销在 6.3 有明显改善[[1]](https://x.com/unity/status/1)。",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://x.com/unity/status/1",
                            "start_index": 0,
                            "end_index": 20,
                            "title": "1",
                        },
                        {
                            "type": "url_citation",
                            "url": "https://docs.unity3d.com/6000.3/perf",
                            "start_index": 21,
                            "end_index": 40,
                            "title": "2",
                        },
                    ],
                }
            ],
        }
    ]
}


def test_extracts_text_and_structured_sources():
    text, sources = sources_from_responses_payload(RESPONSES_PAYLOAD)
    assert "Unity 6" in text
    assert [s["url"] for s in sources] == [
        "https://x.com/unity/status/1",
        "https://docs.unity3d.com/6000.3/perf",
    ]
    assert all(s["provider"] == "xai" for s in sources)
    assert sources[0]["title"] == "1"


def test_dedupes_repeated_citation_urls():
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": "a",
                        "annotations": [
                            {"type": "url_citation", "url": "https://a.com"},
                            {"type": "url_citation", "url": "https://a.com"},
                        ],
                    }
                ]
            }
        ]
    }
    _, sources = sources_from_responses_payload(payload)
    assert len(sources) == 1


def test_ignores_non_url_citation_annotations():
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": "a",
                        "annotations": [
                            {"type": "file_citation", "url": "https://nope.com"},
                            {"type": "url_citation", "url": "not-a-url"},
                        ],
                    }
                ]
            }
        ]
    }
    _, sources = sources_from_responses_payload(payload)
    assert sources == []


def test_absorbs_flat_citations_array():
    payload = {
        "output_text": "hello",
        "citations": ["https://b.com", {"url": "https://c.com", "title": "C"}],
    }
    text, sources = sources_from_responses_payload(payload)
    assert text == "hello"
    assert [s["url"] for s in sources] == ["https://b.com", "https://c.com"]
    assert sources[1]["title"] == "C"


@pytest.mark.parametrize("garbage", [None, "", [], {}, {"output": "nope"}, 42])
def test_malformed_payloads_degrade_to_empty(garbage):
    assert sources_from_responses_payload(garbage) == ("", [])


def test_hallucinated_citation_card_yields_no_sources():
    """回归用例：legacy 模式下模型编造的 citation_card 不带 URL。

    这正是直连 api.x.ai 时观察到的行为 —— 正文看起来引经据典，
    实际零信源。结构化解析必须如实返回 0，而不是被文本骗过去。
    """
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": 'Unity 很快 `citation_card{source="Unity Performance Handbook, 2026"}`',
                        "annotations": [],
                    }
                ]
            }
        ]
    }
    text, sources = sources_from_responses_payload(payload)
    assert "citation_card" in text
    assert sources == []


# --------------------------------------------------------------------------
# _build_search_tools
# --------------------------------------------------------------------------


@pytest.fixture
def provider():
    return GrokSearchProvider("https://api.x.ai/v1", "test-key", "grok-4.6")


def test_default_tools_include_both_engines(provider):
    tools = provider._build_search_tools()
    assert [t["type"] for t in tools] == ["x_search", "web_search"]


def test_unset_filters_are_omitted(provider):
    """未设置的过滤字段不能出现在 payload 里 —— 传 null 可能触发 400。"""
    x_tool = provider._build_search_tools(use_web=False)[0]
    assert set(x_tool) == {"type"}


def test_x_handles_are_capped_and_normalized(provider):
    handles = ["@unity"] + [f"acct{i}" for i in range(30)]
    x_tool = provider._build_search_tools(use_web=False, allowed_x_handles=handles)[0]
    assert len(x_tool["allowed_x_handles"]) == 20
    assert x_tool["allowed_x_handles"][0] == "unity"  # 前导 @ 被剥掉


def test_date_bounds_apply_to_both_tools(provider):
    tools = provider._build_search_tools(from_date="2026-08-01", to_date="2026-08-23")
    for tool in tools:
        assert tool["from_date"] == "2026-08-01"
        assert tool["to_date"] == "2026-08-23"


def test_domain_filters_only_on_web_tool(provider):
    tools = provider._build_search_tools(allowed_domains=["docs.unity3d.com"])
    x_tool, web_tool = tools
    assert "allowed_domains" not in x_tool
    assert web_tool["allowed_domains"] == ["docs.unity3d.com"]


def test_no_engines_enabled_raises(provider):
    assert provider._build_search_tools(use_web=False, use_x=False) == []


# --------------------------------------------------------------------------
# 回退判定与 platform 识别
# --------------------------------------------------------------------------


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.x.ai/v1/responses")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize("status", [400, 404, 405, 422, 501])
def test_unsupported_endpoint_statuses_trigger_fallback(status):
    assert is_endpoint_unsupported(_http_error(status)) is True


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_failures_do_not_trigger_fallback(status):
    """限流和 5xx 是临时故障，应该走重试，不该退化成 legacy 模式。"""
    assert is_endpoint_unsupported(_http_error(status)) is False


def test_non_http_errors_do_not_trigger_fallback():
    assert is_endpoint_unsupported(ValueError("nope")) is False


@pytest.mark.parametrize("platform", ["X", "twitter", "Reddit, X", "tweets"])
def test_platform_naming_x_is_detected(platform):
    assert _mentions_x(platform) is True


@pytest.mark.parametrize("platform", ["", "GitHub", "example.com", "Reddit", "xbox"])
def test_platform_not_naming_x(platform):
    """'x' 作为子串出现在 example/xbox 里时不能误判。"""
    assert _mentions_x(platform) is False
