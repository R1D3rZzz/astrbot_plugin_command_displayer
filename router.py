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
    每条命令包含：命令名、参数、参数说明、命令描述、过滤器类型、使用示例。
    使用缩进和标记让 LLM 更容易解析。
    """
    lines: List[str] = []

    for plugin in plugins:
        pname = plugin.get("name", "")
        pdesc = plugin.get("description", "")
        header = f"## {pname}"
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

            # 使用示例（根据参数自动生成）
            example = _generate_example(name, args, args_desc)
            if example:
                line += f"\n       示例: {example}"

            cmd_lines.append(line)

        block = header + "\n" + "\n".join(cmd_lines)

        # 截断检查
        current_len = sum(len(l) + 1 for l in lines)
        if current_len + len(block) + 1 > max_chars:
            lines.append("...(后续内容已截断)")
            break
        lines.append(block)

    return "\n".join(lines)


def _generate_example(command: str, args: str, args_desc: str) -> str:
    """根据命令名和参数自动生成使用示例"""
    if not args:
        return f"{command}"

    # 解析参数，生成示例值
    example_args = []
    # 提取方括号中的参数名
    import re
    param_names = re.findall(r'\[([^\]]+)\]', args)
    # 也提取 -x 形式的 flag
    flags = re.findall(r'(-\w)', args)

    for pname in param_names:
        pname_lower = pname.lower()
        # 根据参数名生成语义化示例值
        if any(k in pname_lower for k in ("城市", "地点", "city")):
            example_args.append("北京")
        elif any(k in pname_lower for k in ("日期", "时间", "date", "time")):
            example_args.append("今天")
        elif any(k in pname_lower for k in ("插件", "plugin", "名称", "name")):
            example_args.append("天气查询")
        elif any(k in pname_lower for k in ("天数", "数量", "count", "num")):
            example_args.append("3")
        elif any(k in pname_lower for k in ("关键词", "keyword", "搜索", "search", "query")):
            example_args.append("你好")
        elif any(k in pname_lower for k in ("用户", "user", "uid", "id")):
            example_args.append("123456")
        elif any(k in pname_lower for k in ("子命令", "subcmd", "sub")):
            example_args.append("all")
        else:
            # 通用示例值
            example_args.append(f"<{pname}>")

    # 组合示例
    if example_args:
        return f"{command} {' '.join(example_args)}"
    elif flags:
        return f"{command} {flags[0]}"
    else:
        return f"{command}"


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
    "7. 如果用户意图模糊但可能有多个匹配，返回 Top-3 候选（用换行分隔多条结果）\n"
    "\n"
    "【参数提取规则】\n"
    "- 从用户输入中提取命令所需的参数值（如城市名、日期、关键词等）\n"
    "- 只提取命令索引中「参数」列声明的参数\n"
    "- 如果用户没有提供足够参数，提取已有的部分，缺少的留空\n"
    "- 无参数的命令，参数栏填 \"-\"\n"
    "\n"
    "【返回格式】\n"
    "严格返回一行或多行（Top-N 候选时），每行用 | 分隔，共 4 段：\n"
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
    "用户: 帮我查天气（模糊意图，多个天气命令）\n"
    "返回: 天气查询 | /天气 | - | 将查询天气\n"
    "       天气查询 | /forecast | - | 将查询天气预报\n"
    "       天气查询 | /weather | - | 将查看天气信息\n"
    "\n"
    "【注意】\n"
    "- 只返回一行或多行（Top-N 时），不要返回任何其他内容（不要解释、不要 markdown）\n"
    "- 插件名和命令名必须与索引中完全一致\n"
    "- 如果用户意图是「查看/列出/显示」某插件的命令，命令名填 \"-\"\n"
    "- Top-N 候选最多返回 3 条，按匹配度从高到低排列\n"
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
    """解析 LLM 返回的路由结果行。支持单行和多行（Top-N 候选）。"""
    raw = raw.strip()

    if raw.upper().startswith("NONE"):
        return None
    if raw.upper().startswith("LIST_ALL"):
        return ("__list_all__", "-", "-", "将为你展示所有命令")

    # 按行分割，过滤空行
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return None

    # 解析每一行，收集所有有效候选
    candidates: List[RouteResult] = []
    for line in lines:
        parsed = _parse_single_route_line(line, plugins)
        if parsed:
            candidates.append(parsed)

    if not candidates:
        return None

    # 返回第一个（最匹配的）候选
    # 如果有多个候选，日志记录供调试
    if len(candidates) > 1:
        logger.debug(f"LLM 返回 {len(candidates)} 个路由候选，使用第一个")
    return candidates[0]


def _parse_single_route_line(
    raw: str, plugins: List[IndexPlugin]
) -> Optional[RouteResult]:
    """解析单行路由结果"""
    # 解析: plugin_name | command_name | args | message
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        parts = [p.strip() for p in raw.split("\t")]
    if len(parts) < 4:
        # 只有插件名
        cache_key = _match_plugin(parts[0], plugins)
        if cache_key:
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


# 子串匹配的最小长度阈值：猜测词和插件名归一化后至少 2 个字符才允许子串匹配
_MIN_SUBSTRING_LEN = 2


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

    # 策略 3：子串匹配 name（带长度阈值，防止短查询误命中）
    if len(norm_guess) >= _MIN_SUBSTRING_LEN:
        for p in plugins:
            pname = p.get("name", "")
            if not pname:
                continue
            pnorm = _normalize(pname)
            if not pnorm:
                continue
            # 双向子串匹配，但要求较短的一方长度 >= 阈值
            shorter = min(len(norm_guess), len(pnorm))
            if shorter >= _MIN_SUBSTRING_LEN and (pnorm in norm_guess or norm_guess in pnorm):
                return _get_key(p)

    return None


def _match_command(guess: str, plugin_key: str, plugins: List[IndexPlugin]) -> Optional[str]:
    """验证命令是否存在于指定插件中（plugin_key 可以是 name 或 key）。
    支持归一化匹配：忽略 / 前缀、大小写、空格差异。
    """
    norm_guess = _normalize(guess)
    for p in plugins:
        if p.get("name") == plugin_key or p.get("key", "") == plugin_key:
            for cmd in p.get("commands", []):
                cmd_name = cmd.get("command", "")
                # 精确匹配
                if cmd_name.lower() == guess.lower():
                    return cmd_name
                # 归一化匹配（忽略 / 前缀、大小写、空格）
                if _normalize(cmd_name) == norm_guess:
                    return cmd_name
            return None
    return None


# ── LLM 全权代理 ──────────────────────────────────

# 全权代理模式下的最大字符数（比普通路由更大，因为要发送全部缓存）
_MAX_FULL_PROXY_CHARS = 16000

_FULL_PROXY_PROMPT = (
    "你是一个 AstrBot 指令全权代理助手。用户输入了一段自然语言描述其意图，"
    "你需要从下面的完整命令缓存中找出最合适的命令并决定如何执行。\n"
    "\n"
    "【你的任务】\n"
    "1. 理解用户的真实意图\n"
    "2. 从命令缓存中找出最匹配的一条或多条命令\n"
    "3. 决定是否需要执行、执行哪条、带什么参数\n"
    "4. 如果用户只是想查看信息（如\"有哪些命令\"、\"查看xx插件\"），返回展示命令\n"
    "5. 如果用户想执行操作（如\"帮我查天气\"、\"扫描所有插件\"），返回执行命令\n"
    "\n"
    "【返回格式】\n"
    "严格返回一行，用 | 分隔，共 5 段：\n"
    "  动作 | 插件名 | 命令名 | 参数 | 说明\n"
    "\n"
    "动作：EXEC（执行命令）| SHOW（展示插件命令列表）| LIST_ALL（展示全部）| NONE（无法匹配）\n"
    "插件名：命令缓存中 ## 后面的名称\n"
    "命令名：如 /天气、/扫描 等，展示时填 \"-\"\n"
    "参数：执行时需要的参数值，没有则填 \"-\"\n"
    "说明：一句话告诉用户你要做什么，30 字以内\n"
    "\n"
    "【返回示例】\n"
    "用户: 帮我查北京天气\n"
    "返回: EXEC | 天气查询 | /天气 | 北京 | 将为你查询北京的实时天气\n"
    "\n"
    "用户: 扫描所有插件\n"
    "返回: EXEC | Command Displayer | /扫描 | all | 将执行全量扫描所有插件\n"
    "\n"
    "用户: 查看天气插件有哪些命令\n"
    "返回: SHOW | 天气查询 | - | - | 将展示天气查询插件的所有命令\n"
    "\n"
    "用户: 列出所有可用命令\n"
    "返回: LIST_ALL | - | - | - | 将为你展示所有可用命令\n"
    "\n"
    "用户: 今天吃什么好\n"
    "返回: NONE | - | - | - | 抱歉，没有找到匹配的命令\n"
    "\n"
    "【注意】\n"
    "- 只返回一行，不要返回任何其他内容\n"
    "- 插件名和命令名必须与缓存中完全一致\n"
    "- 如果有多条命令都能满足用户需求，选择最具体、最常用的那条\n"
    "- 参数提取要准确，从用户输入中获取具体值\n"
    "\n"
    "【完整命令缓存】\n{cache}\n"
    "\n"
    "【用户输入】\n{query}\n"
    "\n"
    "请返回："
)


async def llm_resolve_all(
    query: str,
    data: Dict[str, dict],
    provider,
) -> Optional[Tuple[str, str, str, str, str]]:
    """
    LLM 全权代理：将全部命令缓存发送给 LLM，让 LLM 自己解析并选择执行。

    返回 (action, plugin_name, command_name, args_str, message) 或 None。
    action 为 EXEC / SHOW / LIST_ALL / NONE。
    """
    if not data or not provider:
        return None

    # 构建完整的命令缓存文本（包含所有信息）
    plugins = _dict_to_index(data)
    cache_text = build_index_text(plugins, max_chars=_MAX_FULL_PROXY_CHARS)
    if not cache_text:
        return None

    prompt = _FULL_PROXY_PROMPT.format(cache=cache_text, query=query)

    try:
        resp = await provider.text_chat(prompt, temperature=0.1)
        raw = _extract_response_text(resp)
        if not raw:
            return None
        return _parse_full_proxy_result(raw, plugins)

    except Exception:
        logger.exception("LLM 全权代理路由异常")
        return None


def _parse_full_proxy_result(
    raw: str, plugins: List[IndexPlugin]
) -> Optional[Tuple[str, str, str, str, str]]:
    """解析全权代理 LLM 返回的结果"""
    raw = raw.strip()

    if raw.upper().startswith("NONE"):
        return ("NONE", "-", "-", "-", "抱歉，没有找到匹配的命令")

    if raw.upper().startswith("LIST_ALL"):
        return ("LIST_ALL", "-", "-", "-", "将为你展示所有可用命令")

    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 5:
        parts = [p.strip() for p in raw.split("\t")]
    if len(parts) < 5:
        return None

    action, plugin_part, cmd_part, args_part, msg_part = parts[0], parts[1], parts[2], parts[3], parts[4]

    action = action.upper()
    if action not in ("EXEC", "SHOW", "LIST_ALL", "NONE"):
        return None

    if action == "LIST_ALL":
        return ("LIST_ALL", "-", "-", "-", msg_part or "将为你展示所有可用命令")

    if action == "NONE":
        return ("NONE", "-", "-", "-", msg_part or "抱歉，没有找到匹配的命令")

    # 匹配插件
    cache_key = _match_plugin(plugin_part, plugins)
    found_key = cache_key or plugin_part
    display = plugin_part
    for p in plugins:
        if p.get("name") == plugin_part or p.get("key", "") == cache_key or p.get("name", "") == cache_key:
            display = p.get("name", plugin_part)
            found_key = p.get("key", "") or p.get("name", "")
            break

    cmd_name = cmd_part if cmd_part != "-" else ""
    args_str = args_part if args_part != "-" else ""

    if action == "EXEC" and cmd_name:
        # 验证命令
        matched = _match_command(cmd_name, found_key, plugins)
        if matched:
            return (action, found_key, matched, args_str,
                    msg_part or f"将执行 **{display}** 的 `{matched}` 命令")
        # 命令未匹配到，降级为 SHOW
        return ("SHOW", found_key, "-", "",
                f"找到插件 **{display}**，将展示其所有命令")

    if action == "SHOW":
        return ("SHOW", found_key, "-", "",
                msg_part or f"将展示 **{display}** 的所有命令")

    return (action, found_key, cmd_name, args_str,
            msg_part or f"匹配到 **{display}**")


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
