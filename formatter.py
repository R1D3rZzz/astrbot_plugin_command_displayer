"""输出格式化：插件/命令列表的三种展示格式"""

from typing import Dict

from .models import (
    CommandEntry, PluginInfo,
    SOURCE_TAGS, FILTER_TYPE_TAGS,
)


# ── 格式化输出 ──────────────────────────────────────


def format_all(
    data: Dict[str, PluginInfo], max_commands: int = 200, fmt: str = "detailed"
) -> str:
    """格式化所有插件的命令汇总"""
    total_commands = sum(len(p.get("commands", [])) for p in data.values())
    direct_count = sum(1 for v in data.values() if v.get("source") == "direct")
    llm_count = sum(1 for v in data.values() if v.get("source") == "llm")

    lines = [
        "[OK] 成功获取行为列表",
        f"  总计: {total_commands} 个行为，{len(data)} 个插件"
        + (f"（直接读取 {direct_count} + LLM解析 {llm_count}）" if llm_count else ""),
    ]

    sorted_plugins = sorted(data.items())
    for idx, (pname, plugin) in enumerate(sorted_plugins, 1):
        cmds = plugin.get("commands", [])
        if not cmds:
            continue

        display_name = plugin.get("name", "")
        tag = _source_tag(plugin.get("source", ""))
        desc = plugin.get("description", "")
        cmd_count = len(cmds)

        if display_name and display_name != pname:
            header = f"--- [{idx}/{len(sorted_plugins)}] {display_name} ({pname}) {tag} ---"
        else:
            header = f"--- [{idx}/{len(sorted_plugins)}] {pname} {tag} ---"

        lines.append("")
        lines.append(header)
        if desc:
            lines.append(f"  描述: {desc}")
        lines.append(f"  共 {cmd_count} 条命令:")
        lines.append("")

        for cmd in cmds[:max_commands]:
            lines.append(_fmt_cmd(cmd, fmt))

    lines.append("")
    lines.append(f"=== 共 {len(sorted_plugins)} 个插件，{total_commands} 条命令 ===")

    return "\n".join(lines)


def format_plugin_list(data: Dict[str, PluginInfo]) -> str:
    """列出所有插件名称及命令数量"""
    total_commands = sum(len(p.get("commands", [])) for p in data.values())
    lines = [f"已加载 {len(data)} 个插件，共 {total_commands} 条命令："]

    for i, (pname, plugin) in enumerate(sorted(data.items()), 1):
        display_name = plugin.get("name", "")
        tag = _source_tag(plugin.get("source", ""))
        cmd_count = len(plugin.get("commands", []))
        desc = plugin.get("description", "")
        desc_short = f" | {desc}" if desc else ""
        name_str = f"{display_name} ({pname})" if display_name and display_name != pname else pname
        lines.append(f"  {i:>2}. {name_str} {tag} ({cmd_count}条){desc_short}")

    lines.append(f"\n共 {len(data)} 个插件，{total_commands} 条命令")
    return "\n".join(lines)


def format_plugin(
    name: str, plugin: PluginInfo, max_commands: int = 200, fmt: str = "detailed"
) -> str:
    """格式化单个插件的命令"""
    display_name = plugin.get("name", "")
    tag = _source_tag(plugin.get("source", ""))
    desc = plugin.get("description", "")
    cmds = plugin.get("commands", [])[:max_commands]
    total = len(plugin.get("commands", []))

    title = f"{display_name} ({name})" if display_name and display_name != name else name

    lines = [f"========== [{title}] {tag} =========="]
    if desc:
        lines.append(f"  描述: {desc}")
    lines.append(f"  共 {total} 条命令:")
    lines.append("")

    for cmd in cmds:
        lines.append(_fmt_cmd(cmd, fmt))

    lines.append("")
    lines.append(f"========== [{title}] 共 {total} 条命令 ==========")
    return "\n".join(lines)


# ── 单条命令格式化 ──────────────────────────────────


def _fmt_cmd(cmd: CommandEntry, fmt: str = "detailed") -> str:
    """格式化单条命令"""
    ftype = cmd.get("filter_type", "")
    tag = FILTER_TYPE_TAGS.get(ftype, "[其他]")

    args = f" [{cmd.get('args')}]" if cmd.get("args") else ""
    desc = cmd.get("description", "") or ""

    if ftype == "regex":
        cmd_text = f"正则: {cmd['command']}"
    elif ftype == "on_all_message":
        cmd_text = "[监听所有消息]"
    elif ftype == "command":
        cmd_text = f"/{cmd['command']}" if not cmd["command"].startswith("/") else cmd["command"]
    elif ftype and ftype not in ("command", "unknown"):
        cmd_text = f"[{ftype}] {cmd['command']}"
    else:
        cmd_text = cmd["command"]

    alias_str = f" (别名: {', '.join(cmd['aliases'])})" if cmd.get("aliases") else ""

    if fmt == "simple":
        return f"    {tag} {cmd_text}{args}{alias_str}"
    if fmt == "table":
        return f"    | {tag} {cmd_text} | {args} | {desc} |"
    return f"    {tag} {cmd_text}{args}{alias_str} - {desc}"


# ── 工具 ──────────────────────────────────────


def _source_tag(source: str) -> str:
    """数据来源标记"""
    return SOURCE_TAGS.get(source, "")
