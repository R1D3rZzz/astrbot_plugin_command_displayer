"""LLM 命令路由 + 命令自动执行

路由逻辑：
1. 从 command_index.json 读取结构化命令索引
2. 将索引文本发送给 LLM，匹配到具体命令（插件名 + 命令名 + 参数）
3. 根据执行模式（auto/confirm）决定是否自动执行

命令执行：
- 通过事件队列注入模拟用户发送命令
- 参考 AstrBot 内置插件的 copy + put_nowait 模式
"""

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.core.message.components import Plain

from .models import INDEX_FILE_PATH, IndexPlugin


# ── 数据结构 ──────────────────────────────────────

# LLM 路由结果: (plugin_name, command_name, args_str, confirmation_message)
RouteResult = Tuple[str, str, str, str]

# 命令索引最大字符数（防止超出 LLM 上下文窗口）
MAX_INDEX_CHARS = 8000


# ── 命令索引加载 ──────────────────────────────────


def load_command_index() -> List[IndexPlugin]:
    """从 command_index.json 加载命令索引"""
    path = Path(INDEX_FILE_PATH)
    if not path.exists():
        logger.warning("command_index.json 不存在，请先执行 /扫描 all")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("plugins", [])
    except Exception as e:
        logger.error(f"加载命令索引失败: {e}")
        return []


def build_index_text(plugins: List[IndexPlugin], max_chars: int = MAX_INDEX_CHARS) -> str:
    """
    将命令索引构建为 LLM 可读的文本格式。
    每条命令包含：命令名、参数、参数说明、命令描述、过滤器类型。
    使用缩进和标记让 LLM 更容易解析。
    """
    lines: List[str] = []

    for plugin in plugins:
        pname = plugin.get("name", "")
        pdesc = plugin.get("description", "")
        header = f"## { pname}"
        if pdesc:
            header += f"\n   描述: {pdesc}"

        cmd_lines: List[str] = []
        for cmd in plugin.get("commands", []):
            name = cmd.get("command", "")
            args = cmd.get("args", "").strip()
            args_desc = cmd.get("args_description", "").strip()
            cmd_desc = cmd.get("description", "").strip()
            ftype = cmd.get("filter_type", "command")

            # 命令行：- /命令名 [参数]
            line = f"  - {name}"
            if args:
                line += f" {args}"

            # 描述行（缩进对齐）
            detail_parts = []
            if cmd_desc and cmd_desc != "无描述":
                detail_parts.append(f"功能: {cmd_desc}")
            if args_desc:
                detail_parts.append(f"参数: {args_desc}")
            if ftype and ftype not in ("command",):
                detail_parts.append(f"类型: {ftype}")
            if detail_parts:
                line += f"\n       {'; '.join(detail_parts)}"

            cmd_lines.append(line)

        block = header + "\n" + "\n".join(cmd_lines)

        # 截断检查
        current_len = sum(len(l) + 1 for l in lines)
        if current_len + len(block) + 1 > max_chars:
            lines.append("...(后续内容已截断)")
            break
        lines.append(block)

    return "\n".join(lines)


# ── LLM 命令级路由 ──────────────────────────────────

_ROUTE_PROMPT = (
    "你是一个 AstrBot 指令路由助手。用户输入了一段自然语言，你需要从下面的命令索引中"
    "找出最匹配的一条具体命令。\n"
    "\n"
    "【匹配策略】\n"
    "1. 首先理解用户意图：是想执行某个操作，还是查询某个插件/命令的信息？\n"
    "2. 优先匹配「功能描述」与用户意图最吻合的命令\n"
    "3. 如果多个命令都匹配，选择最具体的那条（而非笼统的 help/list 类命令）\n"
    "4. 如果用户只是想看某插件的所有命令列表，命令名填 \"-\"\n"
    "5. 如果用户想看全部命令，返回 LIST_ALL\n"
    "6. 完全无法匹配时返回 NONE\n"
    "\n"
    "【参数提取规则】\n"
    "- 从用户输入中提取命令所需的参数值（如城市名、日期、关键词等）\n"
    "- 只提取命令索引中「参数」列声明的参数\n"
    "- 如果用户没有提供足够参数，提取已有的部分，缺少的留空\n"
    "- 无参数的命令，参数栏填 \"-\"\n"
    "\n"
    "【返回格式】\n"
    "严格返回一行，用 | 分隔，共 4 段：\n"
    "  插件名 | 命令名 | 参数 | 简短确认语\n"
    "\n"
    "插件名：命令索引中 ## 后面的名称（如 天气查询、Command Displayer）\n"
    "命令名：如 /天气、/扫描、正则:xxx 等，不带引号\n"
    "参数：从用户输入中提取的参数值，多个参数用空格分隔，没有则填 \"-\"\n"
    "简短确认语：一句话告诉用户匹配到了什么，20 字以内\n"
    "\n"
    "【返回示例】\n"
    "用户: 帮我查北京天气\n"
    "返回: 天气查询 | /天气 | 北京 | 将查询北京的天气\n"
    "\n"
    "用户: 扫描所有插件\n"
    "返回: Command Displayer | /扫描 | all | 将执行全量扫描\n"
    "\n"
    "用户: 查看天气插件有哪些命令\n"
    "返回: 天气查询 | - | - | 将展示天气查询插件的所有命令\n"
    "\n"
    "用户: 列出所有命令\n"
    "返回: LIST_ALL | - | - | 将为你展示所有命令\n"
    "\n"
    "用户: 今天吃什么好\n"
    "返回: NONE | - | - | -\n"
    "\n"
    "【注意】\n"
    "- 只返回一行，不要返回任何其他内容（不要解释、不要 markdown）\n"
    "- 插件名和命令名必须与索引中完全一致\n"
    "- 如果用户意图是「查看/列出/显示」某插件的命令，命令名填 \"-\"\n"
    "\n"
    "【命令索引】\n{index}\n"
    "\n"
    "【用户输入】\n{query}\n"
    "\n"
    "请返回："
)


async def llm_resolve(
    query: str,
    data: Dict[str, dict],
    provider,
) -> Optional[RouteResult]:
    """
    使用 LLM 将自然语言路由到具体命令。

    返回 (plugin_name, command_name, args_str, confirmation_message) 或 None。
    plugin_name == "__list_all__" 表示用户想看全部。
    """
    if not data or not provider:
        return None

    # 从缓存数据构建索引文本
    plugins = _dict_to_index(data)
    index_text = build_index_text(plugins)
    if not index_text:
        return None

    prompt = _ROUTE_PROMPT.format(index=index_text, query=query)

    try:
        resp = await provider.text_chat(prompt, temperature=0.1)
        raw = _extract_response_text(resp)
        if not raw:
            return None
        return _parse_route_result(raw, plugins)

    except Exception:
        logger.exception("LLM 路由异常")
        return None


def _dict_to_index(data: Dict[str, dict]) -> List[IndexPlugin]:
    """将内存中的插件 dict 转换为 IndexPlugin 列表"""
    result: List[IndexPlugin] = []
    for pname, pinfo in sorted(data.items()):
        commands = []
        for cmd in pinfo.get("commands", []):
            commands.append({
                "command": cmd.get("command", ""),
                "args": cmd.get("args", ""),
                "args_description": cmd.get("args_description", ""),
                "description": cmd.get("description", "无描述"),
                "filter_type": cmd.get("filter_type", "command"),
            })
        result.append({
            # 显示名（用于 LLM 匹配）
            "name": pinfo.get("name", pname),
            # 缓存 key（用于 data.get() 查找）
            "key": pname,
            "description": pinfo.get("description", ""),
            "source": pinfo.get("source", "direct"),
            "commands": commands,
        })
    return result


def _extract_response_text(resp) -> Optional[str]:
    """从 LLMResponse 对象中提取文本内容"""
    if not resp:
        return None
    if hasattr(resp, "result_chain") and resp.result_chain:
        chain = getattr(resp.result_chain, "chain", None)
        if chain:
            text = "".join(getattr(c, "text", "") for c in chain)
            if text.strip():
                return text.strip()
    for attr in ("result", "content"):
        if hasattr(resp, attr):
            text = str(getattr(resp, attr)).strip()
            if text:
                return text
    text = str(resp).strip()
    return text if text else None


def _parse_route_result(
    raw: str, plugins: List[IndexPlugin]
) -> Optional[RouteResult]:
    """解析 LLM 返回的路由结果行"""
    raw = raw.strip()

    if raw.upper().startswith("NONE"):
        return None
    if raw.upper().startswith("LIST_ALL"):
        return ("__list_all__", "-", "-", "将为你展示所有命令")

    # 解析: plugin_name | command_name | args | message
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        parts = [p.strip() for p in raw.split("\t")]
    if len(parts) < 4:
        # 只有插件名
        cache_key = _match_plugin(parts[0], plugins)
        if cache_key:
            # 查找显示名
            display = cache_key
            for p in plugins:
                if p.get("key", "") == cache_key or p.get("name", "") == cache_key:
                    display = p.get("name", cache_key)
                    break
            return (cache_key, "-", "", f"找到插件 **{display}**，将展示其所有命令")
        return None

    plugin_part, cmd_part, args_part, msg_part = parts[0], parts[1], parts[2], parts[3]

    cache_key = _match_plugin(plugin_part, plugins)

    # 查找插件显示名（优先用 name 匹配，也接受 key 匹配）
    display = plugin_part
    found_key = cache_key or plugin_part
    for p in plugins:
        if p.get("name") == plugin_part or p.get("key", "") == cache_key or p.get("name", "") == cache_key:
            display = p.get("name", plugin_part)
            found_key = p.get("key", "") or p.get("name", "")
            break

    if not cache_key:
        # 降级：用 LLM 返回的原始名称
        return (found_key, cmd_part if cmd_part != "-" else "",
                args_part if args_part != "-" else "",
                msg_part or f"匹配到 **{display}**")

    cmd_name = cmd_part if cmd_part != "-" else ""
    args_str = args_part if args_part != "-" else ""

    if cmd_name:
        # 验证命令是否存在（用 key 或 name 匹配插件）
        matched = _match_command(cmd_name, found_key, plugins)
        if matched:
            confirm = msg_part or f"匹配到 **{display}** 的 `{matched}` 命令"
            return (found_key, matched, args_str, confirm)
        confirm = msg_part or f"找到插件 **{display}**"
        return (found_key, "-", "", confirm)

    return (found_key, "-", "", msg_part or f"找到插件 **{display}**")


def _normalize(s: str) -> str:
    """归一化名称用于模糊比较：去空格、去常见分隔符、转小写"""
    return s.lower().replace(" ", "").replace("_", "").replace("-", "").replace("（", "(").replace("）", ")")


def _match_plugin(guess: str, plugins: List[IndexPlugin]) -> Optional[str]:
    """
    将 LLM 返回的插件名匹配到实际插件，返回缓存 key（p["key"] 或 p["name"]）。
    支持多种模糊策略，按精确度从高到低依次尝试。
    """
    if not guess:
        return None
    guess = guess.strip()

    def _get_key(p: IndexPlugin) -> str:
        return p.get("key", "") or p.get("name", "")

    # 策略 1：精确匹配 name（忽略大小写）
    for p in plugins:
        pname = p.get("name", "")
        if pname and (pname == guess or pname.lower() == guess.lower()):
            return _get_key(p)

    # 策略 2：归一化匹配 name（忽略空格、分隔符、大小写）
    norm_guess = _normalize(guess)
    for p in plugins:
        pname = p.get("name", "")
        if pname and _normalize(pname) == norm_guess:
            return _get_key(p)

    # 策略 3：子串匹配 name
    for p in plugins:
        pname = p.get("name", "")
        if not pname:
            continue
        pnorm = _normalize(pname)
        if pnorm and (pnorm in norm_guess or norm_guess in pnorm):
            return _get_key(p)

    # 策略 4：编辑距离兜底（更宽松的阈值）
    all_names = [(p.get("name", ""), _get_key(p)) for p in plugins if p.get("name")]
    scored = sorted(
        ((_edit_distance(norm_guess, _normalize(n)), n, k) for n, k in all_names),
        key=lambda x: x[0],
    )
    for dist, _name, key in scored[:3]:
        threshold = min(max(len(norm_guess), len(_normalize(_name))) * 2 // 5, 6)
        if dist <= threshold:
            return key

    return None


def _match_command(guess: str, plugin_key: str, plugins: List[IndexPlugin]) -> Optional[str]:
    """验证命令是否存在于指定插件中（plugin_key 可以是 name 或 key）"""
    for p in plugins:
        if p.get("name") == plugin_key or p.get("key", "") == plugin_key:
            for cmd in p.get("commands", []):
                cmd_name = cmd.get("command", "")
                if cmd_name.lower() == guess.lower():
                    return cmd_name
                if cmd_name.lstrip("/").lower() == guess.lstrip("/").lower():
                    return cmd_name
            return None
    return None


# ── 命令输错提醒 ──────────────────────────────────


def suggest_correction(query: str, candidates: List[str], top_n: int = 3) -> List[str]:
    """基于编辑距离，返回最相似的候选名列表"""
    if not candidates or not query:
        return []
    q = query.lower()
    scored = sorted(
        ((_edit_distance(q, c.lower()), c) for c in candidates),
        key=lambda x: x[0],
    )
    return [c for _, c in scored[:top_n]]


def _edit_distance(s1: str, s2: str) -> int:
    """编辑距离（Levenshtein），空间优化"""
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


# ── 命令自动执行 ──────────────────────────────────


def execute_command(event, command_name: str, args_str: str, context) -> bool:
    """
    模拟用户发送命令，通过事件队列注入来自动执行。
    参考 AstrBot 内置插件的 copy + put_nowait 模式。

    command_name: 命令名（不带 /），如 "天气"
    args_str: 参数，如 "北京"
    """
    try:
        cmd_text = f"/{command_name}"
        if args_str:
            cmd_text += f" {args_str}"

        new_event = copy.copy(event)
        new_event.message_str = cmd_text
        new_event.message_obj.message_str = cmd_text
        new_event.message_obj.message = [Plain(cmd_text)]
        new_event.is_wake = True
        new_event.is_at_or_wake_command = True
        new_event.clear_result()
        new_event._force_stopped = False

        context.get_event_queue().put_nowait(new_event)
        logger.info(f"事件队列注入: {cmd_text}")
        return True

    except Exception:
        logger.exception("命令执行注入失败")
        return False
