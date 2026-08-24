#!/usr/bin/env python3
"""把本机各 MCP 客户端里的 grok-search 从上游 GuDaStudio/GrokSearch 指向本 fork。

支持 Claude Desktop / Codex / opencode / Antigravity，覆盖 Windows、macOS、Linux
的默认配置路径。

改法是**纯文本替换那一段 URL**，不做解析后重写 —— 这样 JSONC 的注释、TOML 的
排版、以及各家自己的字段（enabled / disabled / type ...）全部原样保留，也不可能
把结构改坏。API key 不会被读取或改动。

    python scripts/migrate_clients.py          # 预演，只报告不写入
    python scripts/migrate_clients.py --apply  # 实际写入（先备份）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

FORK_URL = "git+https://github.com/zhehaosun717/sunami-grok-search"

# 匹配上游 URL，含可选的 .git 后缀和可选的 @分支
UPSTREAM_RE = re.compile(
    r"git\+https://github\.com/GuDaStudio/GrokSearch(?:\.git)?(?:@[\w./-]+)?"
)

HOME = Path.home()


def candidate_files() -> list[tuple[str, Path]]:
    """各客户端的配置路径。不存在的会在后面被过滤掉。"""
    appdata = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
    out: list[tuple[str, Path]] = [
        # Claude Desktop
        ("Claude Desktop", appdata / "Claude/claude_desktop_config.json"),
        ("Claude Desktop", HOME / "Library/Application Support/Claude/claude_desktop_config.json"),
        ("Claude Desktop", HOME / ".config/Claude/claude_desktop_config.json"),
        # Claude Code CLI
        ("Claude Code", HOME / ".claude.json"),
        ("Claude Code", HOME / ".claude/settings.json"),
        # Codex
        ("Codex", HOME / ".codex/config.toml"),
        # opencode
        ("opencode", HOME / ".config/opencode/opencode.json"),
        ("opencode", HOME / ".config/opencode/opencode.jsonc"),
        ("opencode", HOME / ".opencode/opencode.json"),
        # Antigravity
        ("Antigravity", HOME / ".gemini/antigravity-ide/mcp_config.json"),
        ("Antigravity", HOME / ".gemini/antigravity/mcp_config.json"),
        ("Antigravity", HOME / ".gemini/config/mcp_config.json"),
    ]
    seen: set[Path] = set()
    uniq = []
    for label, path in out:
        rp = path.expanduser()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append((label, rp))
    return uniq


def migrate(path: Path, apply: bool) -> tuple[int, str | None]:
    """返回 (替换次数, 错误信息)。"""
    try:
        text = io.open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        return 0, "unreadable: %s" % exc

    matches = UPSTREAM_RE.findall(text)
    if not matches:
        return 0, None

    new_text = UPSTREAM_RE.sub(FORK_URL, text)

    if not apply:
        return len(matches), None

    backup = path.with_name(
        path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    shutil.copy2(path, backup)

    # 纯 .json 才做回读校验；.jsonc 有注释、.toml 不是 JSON，跳过
    if path.suffix == ".json":
        try:
            json.loads(new_text)
        except json.JSONDecodeError as exc:
            return 0, "would produce invalid JSON (%s) -- left untouched" % exc

    io.open(path, "w", encoding="utf-8", newline="\n").write(new_text)
    return len(matches), None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="实际写入。默认只预演。")
    args = ap.parse_args()

    print("fork URL:", FORK_URL)
    print("mode    :", "APPLY (会写入，先备份)" if args.apply else "DRY RUN (不写入)")
    print()

    total = 0
    touched = 0
    for label, path in candidate_files():
        if not path.is_file():
            continue
        count, err = migrate(path, args.apply)
        if err:
            print("  !! %-16s %s\n     %s" % (label, path, err))
            continue
        if count:
            total += count
            touched += 1
            verb = "repointed" if args.apply else "would repoint"
            print("  -> %-16s %s  (%s %d ref%s)"
                  % (label, path, verb, count, "" if count == 1 else "s"))
        else:
            print("     %-16s %s  (no upstream ref)" % (label, path))

    print()
    if not total:
        print("没有找到指向上游的引用。要么已经迁移过，要么这台机器的配置在别处。")
        return 0

    if args.apply:
        print("完成：%d 个文件，%d 处引用。原文件已备份为 *.bak-<时间戳>。" % (touched, total))
        print("重启对应的客户端后生效。")
    else:
        print("预演结果：%d 个文件，%d 处引用。加 --apply 实际写入。" % (touched, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
