"""输出格式化和模糊搜索"""

from typing import Dict, Optional

from .models import (
    PluginInfo,
    CommandEntry,
    SOURCE_TAGS,
    SOURCE_DIRECT,
    SOURCE_LLM,
    FILTER_TYPE_TAGS,
)


# ═══════════════════════════════════════════════════════
# 格式化输出
# ═══════════════════════════════════════════════════════


def format_all(data: Dict[str, PluginInfo], max_commands: int = 200, fmt: str = "detailed") -> str:
    """格式化所有插件的命令汇总"""
    total_commands = sum(len(p.get("commands", [])) for p in data.values())
    direct_count = sum(1 for v in data.values() if v.get("source") == SOURCE_DIRECT)
    llm_count = sum(1 for v in data.values() if v.get("source") == SOURCE_LLM)

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

        # 标题：优先用 display_name，如果与目录名不同则同时显示
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
    lines = [
        f"已加载 {len(data)} 个插件，共 {total_commands} 条命令：",
    ]
    for i, (pname, plugin) in enumerate(sorted(data.items()), 1):
        display_name = plugin.get("name", "")
        tag = _source_tag(plugin.get("source", ""))
        cmd_count = len(plugin.get("commands", []))
        desc = plugin.get("description", "")
        desc_short = f" | {desc}" if desc else ""

        # 有 display_name 且与目录名不同则同时显示
        if display_name and display_name != pname:
            name_str = f"{display_name} ({pname})"
        else:
            name_str = pname

        lines.append(f"  {i:>2}. {name_str} {tag} ({cmd_count}条){desc_short}")
    lines.append(f"\n共 {len(data)} 个插件，{total_commands} 条命令")
    return "\n".join(lines)


def format_plugin(name: str, plugin: PluginInfo, max_commands: int = 200, fmt: str = "detailed") -> str:
    """格式化单个插件的命令"""
    display_name = plugin.get("name", "")
    tag = _source_tag(plugin.get("source", ""))
    desc = plugin.get("description", "")
    cmds = plugin.get("commands", [])[:max_commands]
    total = len(plugin.get("commands", []))

    # 标题：优先用 display_name，如果与目录名不同则同时显示
    if display_name and display_name != name:
        title = f"{display_name} ({name})"
    else:
        title = name

    lines = [
        f"========== [{title}] {tag} ==========",
    ]
    if desc:
        lines.append(f"  描述: {desc}")
    lines.append(f"  共 {total} 条命令:")
    lines.append("")

    for cmd in cmds:
        lines.append(_fmt_cmd(cmd, fmt))

    lines.append("")
    lines.append(f"========== [{title}] 共 {total} 条命令 ==========")
    return "\n".join(lines)


def _fmt_cmd(cmd: CommandEntry, fmt: str = "detailed") -> str:
    """格式化单条命令"""
    ftype = cmd.get("filter_type", "")
    tag = FILTER_TYPE_TAGS.get(ftype, "[其他]")

    args = f" [{cmd.get('args')}]" if cmd.get("args") else ""
    desc = cmd.get("description", "") or ""

    # 构建命令显示文本
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


# ═══════════════════════════════════════════════════════
# 模糊搜索
# ═══════════════════════════════════════════════════════


def fuzzy_find(data: Dict[str, PluginInfo], query: str, threshold: float = 0.6) -> Optional[str]:
    """在插件数据中模糊搜索，返回最佳匹配的插件名"""
    best_score = 0.0
    matched = None
    q = query.lower()
    for name in data:
        score = _fuzzy_score(name, q)
        if score > best_score and score >= threshold:
            best_score = score
            matched = name
    return matched


def _fuzzy_score(text: str, query: str) -> float:
    """计算模糊匹配分数（0.0-1.0）"""
    if query in text:
        return 1.0
    return 1.0 - (_edit_distance(text, query) / max(len(text), len(query)))


def _edit_distance(s1: str, s2: str) -> int:
    """编辑距离（Levenshtein）"""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


# ═══════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════


def _source_tag(source: str) -> str:
    """数据来源标记"""
    return SOURCE_TAGS.get(source, "")
