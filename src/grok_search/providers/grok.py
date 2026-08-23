import httpx
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base
from zoneinfo import ZoneInfo
from .base import BaseSearchProvider, SearchResult
from ..utils import search_prompt, fetch_prompt, url_describe_prompt, rank_sources_prompt, native_search_prompt
from ..logger import log_info
from ..config import config


def get_local_time_info() -> str:
    """获取本地时间信息，用于注入到搜索查询中"""
    try:
        # 尝试获取系统本地时区
        local_tz = datetime.now().astimezone().tzinfo
        local_now = datetime.now(local_tz)
    except Exception:
        # 降级使用 UTC
        local_now = datetime.now(timezone.utc)

    # 格式化时间信息
    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {local_now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {local_now.tzname() or 'Local'}\n"
    )


def _needs_time_context(query: str) -> bool:
    """检查查询是否需要时间上下文"""
    # 中文时间相关关键词
    cn_keywords = [
        "当前", "现在", "今天", "明天", "昨天",
        "本周", "上周", "下周", "这周",
        "本月", "上月", "下月", "这个月",
        "今年", "去年", "明年",
        "最新", "最近", "近期", "刚刚", "刚才",
        "实时", "即时", "目前",
    ]
    # 英文时间相关关键词
    en_keywords = [
        "current", "now", "today", "tomorrow", "yesterday",
        "this week", "last week", "next week",
        "this month", "last month", "next month",
        "this year", "last year", "next year",
        "latest", "recent", "recently", "just now",
        "real-time", "realtime", "up-to-date",
    ]

    query_lower = query.lower()

    for keyword in cn_keywords:
        if keyword in query:
            return True

    for keyword in en_keywords:
        if keyword in query_lower:
            return True

    return False

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _is_retryable_exception(exc) -> bool:
    """检查异常是否可重试"""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


class _WaitWithRetryAfter(wait_base):
    """等待策略：优先使用 Retry-After 头，否则使用指数退避"""

    def __init__(self, multiplier: float, max_wait: int):
        self._base_wait = wait_random_exponential(multiplier=multiplier, max=max_wait)
        self._protocol_error_base = 3.0

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = self._parse_retry_after(exc.response)
                if retry_after is not None:
                    return retry_after
            if isinstance(exc, httpx.RemoteProtocolError):
                return self._base_wait(retry_state) + self._protocol_error_base
        return self._base_wait(retry_state)

    def _parse_retry_after(self, response: httpx.Response) -> Optional[float]:
        """解析 Retry-After 头（支持秒数或 HTTP 日期格式）"""
        header = response.headers.get("Retry-After")
        if not header:
            return None
        header = header.strip()

        if header.isdigit():
            return float(header)

        try:
            retry_dt = parsedate_to_datetime(header)
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            delay = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delay)
        except (TypeError, ValueError):
            return None


class GrokSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4-fast"):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "Grok"

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> List[SearchResult]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        platform_prompt = ""

        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        time_context = get_local_time_info() + "\n"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": search_prompt,
                },
                {"role": "user", "content": time_context + query + platform_prompt},
            ],
            "stream": True,
        }

        await log_info(ctx, f"platform_prompt: { query + platform_prompt}", config.debug_enabled)

        return await self._execute_stream_with_retry(headers, payload, ctx)

    async def fetch(self, url: str, ctx=None) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": fetch_prompt,
                },
                {"role": "user", "content": url + "\n获取该网页内容并返回其结构化Markdown格式" },
            ],
            "stream": True,
        }
        return await self._execute_stream_with_retry(headers, payload, ctx)

    async def _parse_streaming_response(self, response, ctx=None) -> str:
        content = ""
        full_body_buffer = [] 
        
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            
            full_body_buffer.append(line)

            # 兼容 "data: {...}" 和 "data:{...}" 两种 SSE 格式
            if line.startswith("data:"):
                if line in ("data: [DONE]", "data:[DONE]"):
                    continue
                try:
                    # 去掉 "data:" 前缀，并去除可能的空格
                    json_str = line[5:].lstrip()
                    data = json.loads(json_str)
                    choices = data.get("choices", [])
                    if choices and len(choices) > 0:
                        delta = choices[0].get("delta", {})
                        if "content" in delta:
                            content += delta["content"]
                except (json.JSONDecodeError, IndexError):
                    continue
                
        if not content and full_body_buffer:
            try:
                full_text = "".join(full_body_buffer)
                data = json.loads(full_text)
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "")
            except json.JSONDecodeError:
                pass
        
        await log_info(ctx, f"content: {content}", config.debug_enabled)

        return content

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None) -> str:
        """执行带重试机制的流式 HTTP 请求"""
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(config.retry_max_attempts + 1),
                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            ):
                with attempt:
                    async with client.stream(
                        "POST",
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        return await self._parse_streaming_response(response, ctx)

    async def describe_url(self, url: str, ctx=None) -> dict:
        """让 Grok 阅读单个 URL 并返回 title + extracts"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": url_describe_prompt},
                {"role": "user", "content": url},
            ],
            "stream": True,
        }
        result = await self._execute_stream_with_retry(headers, payload, ctx)
        title, extracts = url, ""
        for line in result.strip().splitlines():
            if line.startswith("Title:"):
                title = line[6:].strip() or url
            elif line.startswith("Extracts:"):
                extracts = line[9:].strip()
        return {"title": title, "extracts": extracts, "url": url}

    async def rank_sources(self, query: str, sources_text: str, total: int, ctx=None) -> list[int]:
        """让 Grok 按查询相关度对信源排序，返回排序后的序号列表"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": rank_sources_prompt},
                {"role": "user", "content": f"Query: {query}\n\n{sources_text}"},
            ],
            "stream": True,
        }
        result = await self._execute_stream_with_retry(headers, payload, ctx)
        order: list[int] = []
        seen: set[int] = set()
        for token in result.strip().split():
            try:
                n = int(token)
                if 1 <= n <= total and n not in seen:
                    seen.add(n)
                    order.append(n)
            except ValueError:
                continue
        # 补齐遗漏的序号
        for i in range(1, total + 1):
            if i not in seen:
                order.append(i)
        return order

    # -----------------------------------------------------------------------
    # sunami: 原生工具检索（xAI Responses API）
    #
    # 上游的 search() 走 /chat/completions，payload 里没有任何工具声明，
    # 检索完全依赖上游端点在服务端代劳。直连 api.x.ai 时那条路等于让模型
    # 凭记忆作答。下面这条走 /v1/responses + tools，检索由 xAI 真实执行，
    # 引用以 annotations(url_citation) 结构化返回。
    # -----------------------------------------------------------------------

    def _build_search_tools(
        self,
        use_web: bool = True,
        use_x: bool = True,
        allowed_x_handles: Optional[List[str]] = None,
        excluded_x_handles: Optional[List[str]] = None,
        from_date: str = "",
        to_date: str = "",
        allowed_domains: Optional[List[str]] = None,
        excluded_domains: Optional[List[str]] = None,
    ) -> List[dict]:
        """构造 Responses API 的 tools 数组。只写入实际设置了的过滤字段。"""
        tools: List[dict] = []

        if use_x:
            x_tool: dict = {"type": "x_search"}
            # xAI 文档限制 allowed/excluded handles 各最多 20 个
            if allowed_x_handles:
                x_tool["allowed_x_handles"] = [h.lstrip("@") for h in allowed_x_handles[:MAX_X_HANDLES]]
            if excluded_x_handles:
                x_tool["excluded_x_handles"] = [h.lstrip("@") for h in excluded_x_handles[:MAX_X_HANDLES]]
            if from_date:
                x_tool["from_date"] = from_date
            if to_date:
                x_tool["to_date"] = to_date
            tools.append(x_tool)

        if use_web:
            web_tool: dict = {"type": "web_search"}
            if allowed_domains:
                web_tool["allowed_domains"] = allowed_domains
            if excluded_domains:
                web_tool["excluded_domains"] = excluded_domains
            if from_date:
                web_tool["from_date"] = from_date
            if to_date:
                web_tool["to_date"] = to_date
            tools.append(web_tool)

        return tools

    async def search_native(
        self,
        query: str,
        platform: str = "",
        use_web: bool = True,
        use_x: bool = True,
        allowed_x_handles: Optional[List[str]] = None,
        excluded_x_handles: Optional[List[str]] = None,
        from_date: str = "",
        to_date: str = "",
        allowed_domains: Optional[List[str]] = None,
        excluded_domains: Optional[List[str]] = None,
        ctx=None,
    ) -> dict:
        """走 /v1/responses + 原生检索工具，返回原始响应体。

        解析交给 sources.sources_from_responses_payload —— provider 只负责传输，
        避免 providers 反向依赖 sources 造成循环导入。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        tools = self._build_search_tools(
            use_web=use_web,
            use_x=use_x,
            allowed_x_handles=allowed_x_handles,
            excluded_x_handles=excluded_x_handles,
            from_date=from_date,
            to_date=to_date,
            allowed_domains=allowed_domains,
            excluded_domains=excluded_domains,
        )
        if not tools:
            raise ValueError("search_native 至少需要启用 web_search 或 x_search 之一")

        user_content = query
        if _needs_time_context(query):
            user_content = get_local_time_info() + "\n" + user_content
        if platform:
            user_content += (
                "\n\nFocus your searches on these platforms: " + platform + "\n"
            )

        payload = {
            "model": self.model,
            "instructions": native_search_prompt,
            "input": [{"role": "user", "content": user_content}],
            "tools": tools,
        }

        await log_info(
            ctx,
            f"search_native tools={[t['type'] for t in tools]} model={self.model}",
            config.debug_enabled,
        )

        return await self._execute_json_with_retry(headers, payload, "/responses", ctx)

    async def _execute_json_with_retry(
        self, headers: dict, payload: dict, path: str, ctx=None
    ) -> dict:
        """非流式 JSON 请求，复用与流式路径相同的重试策略。

        带工具的检索耗时明显长于纯生成，read 超时放宽到 180s。
        """
        timeout = httpx.Timeout(connect=6.0, read=180.0, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(config.retry_max_attempts + 1),
                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            ):
                with attempt:
                    response = await client.post(
                        f"{self.api_url}{path}",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json()


MAX_X_HANDLES = 20


def is_endpoint_unsupported(exc) -> bool:
    """判断异常是否表示端点/工具不被支持，用于回退到 chat/completions。

    网关实现不一：不支持 Responses API 时可能返回 404，也可能返回 400/422
    （能路由但拒绝 tools 字段）。这几种都视为"该端点走不通"，而非临时故障。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (400, 404, 405, 422, 501)
    return False
