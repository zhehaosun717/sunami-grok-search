# 一键迁移提示词

在新电脑上把整段复制给 Claude Code / Codex / 任意能跑命令的 agent。
它是自包含的 —— 不需要对方了解这个项目的任何背景。

---

```
帮我把这台电脑上的 grok-search MCP 从上游仓库切换到我自己的 fork。

背景（你不需要验证，照做即可）：
上游 GuDaStudio/GrokSearch 的 web_search 发的是不带工具声明的 chat/completions
请求，检索靠上游网关代劳。直连 api.x.ai 时它不会真的检索，只会让模型凭记忆作答
并编造引用，sources_count 恒为 0。我的 fork 改用 xAI Responses API 的原生
web_search / x_search 工具，引用从 annotations 结构化读取。

fork 地址：https://github.com/zhehaosun717/sunami-grok-search

请按以下步骤做：

1. 把仓库 clone 到临时目录并进入：
   git clone https://github.com/zhehaosun717/sunami-grok-search
   cd sunami-grok-search

2. 先跑预演，把完整输出原样展示给我：
   python scripts/migrate_clients.py
   （Windows 上 python 可能会跳转 Microsoft Store，那就改用 py；
     Mac/Linux 上如果没有 python 就用 python3。脚本只用标准库，无需装依赖。）

3. 看预演结果：
   - 如果列出了 "would repoint" 的文件 → 执行 python scripts/migrate_clients.py --apply
     然后告诉我改了哪几个文件。
   - 如果显示"没有找到指向上游的引用" → 说明这台机器要么已经迁移过，要么根本
     没装 grok-search。先告诉我，别自作主张去装。

4. 告诉我需要重启哪些客户端才能生效（脚本输出里会列出改动的配置文件，
   按文件反推是哪个客户端）。Claude Desktop 需要完全退出进程再打开，
   关窗口不够。

约束：
- 不要读取、打印、修改任何配置文件里的 API key。脚本本身不碰 key，你也别碰。
- 不要手动编辑配置文件，用脚本。脚本做的是纯文本 URL 替换，会保留 JSONC 注释、
  TOML 排版和各客户端自己的字段；手改容易破坏结构。
- 脚本会自动备份成 *.bak-<时间戳>，不用你另外备份。
- 只有我确认过预演输出、或者预演结果明显无误时才加 --apply。
```

---

## 如果那台机器还没装过 grok-search

上面的提示词遇到"没装"的情况会停下来问你。要新装的话，把下面这段接着发给它，
**先把两个 key 填进去**：

```
这台机器还没装 grok-search，帮我加上。

我用的客户端是：<Claude Desktop / Codex / opencode / Antigravity，写你实际用的>

配置内容：
  command: uvx
  args:    --python 3.12 --from git+https://github.com/zhehaosun717/sunami-grok-search grok-search
  env:
    GROK_API_URL  = https://api.x.ai/v1
    GROK_API_KEY  = <填你的 xAI key>
    TAVILY_API_URL = https://api.tavily.com
    TAVILY_API_KEY = <填你的 Tavily key>

各客户端的配置文件位置和字段结构见仓库里的 SUNAMI.md「Migrating an existing
install」那一节的表格。按那个格式写进对应文件，写完让我确认，然后我重启客户端。
```

## 给不认识的 harness/agent 新装（配置格式未知）

上面的模板假设你知道自己用的是哪种客户端、配置长什么样。如果是自建或不常见的
harness（比如某个本地跑的 DeepSeek/Qwen agent 框架），配置文件位置和格式可能
和标准客户端都不一样，你也未必记得。这种情况让跑在那个 harness 里的 agent
自己去找，比你或我瞎猜更可靠 —— 它对自己的运行环境最清楚。

把这段发给那个 harness 里的 agent，**先把两个 key 填进去**：

```
帮我给你自己（当前这个 agent/harness）装一个 MCP server，叫 grok-search。

我不知道你的 MCP 配置文件在哪、长什么格式，所以先别动手写，按下面的顺序来：

1. 找到你自己的 MCP 配置在哪个文件、什么格式（JSON/TOML/YAML/其他），
   如果你不确定，检查你的启动参数、文档、或已经配置好的其他 MCP server
   （如果有的话）作为格式参考。把你找到的文件路径和现有内容（脱敏掉任何
   key/token）贴给我看，先别写。

2. 我确认格式之后，你再照这个内容加一个 server 条目：
   name:    grok-search
   command: uvx
   args:    --python 3.12 --from git+https://github.com/zhehaosun717/sunami-grok-search grok-search
   env:
     GROK_API_URL   = https://api.x.ai/v1
     GROK_API_KEY   = <填你的 xAI key>
     TAVILY_API_URL = https://api.tavily.com
     TAVILY_API_KEY = <填你的 Tavily key>

3. 写之前把原文件备份一份（加时间戳后缀），只在配置文件里新增这一个 server
   条目，不要动其他已有的 server 或字段。

4. 写完把 diff 或改动前后对比给我看，然后告诉我要怎么重启/重新加载你才能
   生效（重启进程？重新加载配置？发某个内部命令？你自己知道该怎么做）。

常见格式供你参考（不一定和你的一致，只是给你个方向）：
  - JSON 风格（Claude Desktop / Antigravity 常见）：
    { "mcpServers": { "grok-search": { "command": "uvx", "args": [...], "env": {...} } } }
  - TOML 风格（Codex 常见）：
    [mcp_servers.grok-search]
    command = "uvx"
    args = [...]
    [mcp_servers.grok-search.env]
    GROK_API_KEY = "..."
  - command 数组风格（opencode 常见）：
    { "mcp": { "grok-search": { "type": "local", "command": ["uvx", "--from", "..."], "environment": {...} } } }

约束：
- 不要把 key 打印在你给我看的确认信息里之外的任何地方（日志、其他文件）。
- 不确定的地方就停下来问，不要猜一个格式就直接写。
```

装完用下面「验证装对了没有」那一节确认。

## 验证装对了没有

重启客户端后，问它：

> 调用 grok-search 的 get_config_info

返回里出现 `GROK_SEARCH_MODE`、`GROK_X_SEARCH`、`GROK_WEB_SEARCH` 这三个字段，
且 `GROK_MODEL` 是 `grok-4.6`，就说明跑的是 fork。上游版本没有这几个字段。

再试一条真正用到 X 检索的：

> 用 grok-search 搜一下 @unity 最近一周在 X 上发了什么

返回的 `search_mode` 应该是 `native`，`sources_count` 大于 0，
`get_sources` 能拿到真实的 x.com 链接。如果 `search_mode` 是 `legacy` 或者
`sources_count` 是 0，说明 key 没有搜索工具权限，或者端点不对。
