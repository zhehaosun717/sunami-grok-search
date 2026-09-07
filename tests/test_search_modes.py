"""web_search 三种模式的集成测试（全部走打桩，不发真实请求）。

native / legacy / auto 的切换和回退是这次改造里控制流最绕的一段，
也是最容易在真实环境才暴露问题的一段，所以单独锁住。
"""

import os

import httpx
import pytest

os.environ.setdefault("GROK_API_URL", "https://api.x.ai/v1")
os.environ.setdefault("GROK_API_KEY", "test-key")

from grok_search import server
from grok_search.providers.grok import GrokSearchProvider

pytestmark = pytest.mark.asyncio


NATIVE_PAYLOAD = {
    "output": [
        {
            "content": [
                {
                    "type": "output_text",
                    "text": "Unity 6.3 降低了 GC 压力。",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://x.com/unity/status/42",
                            "title": "1",
                        }
                    ],
                }
            ]
        }
    ]
}

LEGACY_TEXT = "旧路径答案\n\n## Sources\n- [Unity Docs](https://docs.unity3d.com/perf)"


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.x.ai/v1/responses")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


@pytest.fixture
def stub(monkeypatch):
    """按需给 search_native / search 打桩，并记录调用情况。"""
    calls = {"native": 0, "legacy": 0, "native_kwargs": None}

    def set_native(behaviour):
        async def _native(self, query, platform="", **kwargs):
            calls["native"] += 1
            calls["native_kwargs"] = kwargs
            if isinstance(behaviour, Exception):
                raise behaviour
            return behaviour

        monkeypatch.setattr(GrokSearchProvider, "search_native", _native)

    def set_legacy(text):
        async def _legacy(self, query, platform="", *args, **kwargs):
            calls["legacy"] += 1
            return text

        monkeypatch.setattr(GrokSearchProvider, "search", _legacy)

    calls["set_native"] = set_native
    calls["set_legacy"] = set_legacy
    return calls


async def test_native_mode_uses_structured_citations(stub):
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="native")

    assert result["search_mode"] == "native"
    assert result["sources_count"] == 1
    assert stub["legacy"] == 0, "native 模式不应触碰 legacy 路径"

    sources = await server.get_sources(result["session_id"])
    assert sources["sources"][0]["url"] == "https://x.com/unity/status/42"
    assert sources["sources"][0]["provider"] == "xai"


async def test_legacy_mode_skips_native_entirely(stub):
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="legacy")

    assert result["search_mode"] == "legacy"
    assert stub["native"] == 0
    assert result["sources_count"] == 1  # 从正文里刮出来的那条


async def test_auto_falls_back_when_endpoint_unsupported(stub):
    """网关不支持 Responses API（404）时，auto 必须无声回退而不是报错。"""
    stub["set_native"](_http_error(404))
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="auto")

    assert result["search_mode"] == "legacy"
    assert stub["native"] == 1 and stub["legacy"] == 1
    assert "旧路径答案" in result["content"]


async def test_auto_does_not_fall_back_on_transient_failure(stub):
    """端点可用但这次请求挂了（500），auto 必须报错而不是降级。

    回退的前提是"这个端点走不通"。限流、超时、5xx 都不是那回事 —— 端点
    好好的，只是这一次没成。此时降级到 legacy 换来的不是"退而求其次的检
    索结果"，而是一份凭记忆编造引用的答案，比直接失败更有害。
    """
    stub["set_native"](_http_error(500))
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="auto")

    assert result["search_mode"] == "native"
    assert result["sources_count"] == 0
    assert "native 检索失败" in result["content"]
    assert stub["legacy"] == 0, "暂时性故障不得回退 legacy"


async def test_auto_does_not_fall_back_on_timeout(stub):
    """超时同理：连接不上不等于端点不支持 Responses API。"""
    stub["set_native"](httpx.ReadTimeout("timed out"))
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="auto")

    assert result["search_mode"] == "native"
    assert stub["legacy"] == 0


async def test_native_mode_surfaces_error_instead_of_silent_fallback(stub):
    """显式指定 native 时失败要报出来，不能悄悄降级 —— 否则用户以为在用 X 检索。"""
    stub["set_native"](_http_error(404))
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("unity 性能", mode="native")

    assert result["search_mode"] == "native"
    assert result["sources_count"] == 0
    assert "native 检索失败" in result["content"]
    assert stub["legacy"] == 0


async def test_x_filters_are_forwarded_to_provider(stub):
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    await server.web_search(
        "unity 性能",
        mode="native",
        x_handles="@unity, unity3d",
        exclude_x_handles="spambot",
        from_date="2026-08-01",
        to_date="2026-08-23",
        allowed_domains="docs.unity3d.com",
    )

    kwargs = stub["native_kwargs"]
    assert kwargs["allowed_x_handles"] == ["@unity", "unity3d"]
    assert kwargs["excluded_x_handles"] == ["spambot"]
    assert kwargs["from_date"] == "2026-08-01"
    assert kwargs["to_date"] == "2026-08-23"
    assert kwargs["allowed_domains"] == ["docs.unity3d.com"]


async def test_platform_naming_x_forces_x_search(stub, monkeypatch):
    monkeypatch.setenv("GROK_X_SEARCH", "false")
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    await server.web_search("unity 性能", platform="X", mode="native")

    assert stub["native_kwargs"]["use_x"] is True


async def test_zero_sources_is_reported_honestly(stub):
    """检索真的没结果时，sources_count 必须是 0。

    这是整个改造的核心契约：调用方靠这个数字判断正文可不可信。
    """
    stub["set_native"]({"output": [{"content": [{"type": "output_text",
                                                 "text": "没找到", "annotations": []}]}]})
    stub["set_legacy"](LEGACY_TEXT)

    result = await server.web_search("不存在的东西", mode="native")

    assert result["sources_count"] == 0
    assert result["content"] == "没找到"


async def test_x_handles_narrow_search_to_x_only(stub):
    """给了 handle 却仍开全网搜索，web 噪音会淹没 X 结果（实测如此）。"""
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    await server.web_search("posts about the engine", mode="native", x_handles="unity")

    kwargs = stub["native_kwargs"]
    assert kwargs["use_x"] is True
    assert kwargs["use_web"] is False


async def test_explicit_domain_filter_keeps_web_enabled(stub):
    """调用方显式给了域名过滤，说明确实想要 web，不做收窄。"""
    stub["set_native"](NATIVE_PAYLOAD)
    stub["set_legacy"](LEGACY_TEXT)

    await server.web_search(
        "unity perf", mode="native", x_handles="unity", allowed_domains="unity.com"
    )

    assert stub["native_kwargs"]["use_web"] is True
