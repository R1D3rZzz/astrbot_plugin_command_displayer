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
import re
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.core.message.components import Plain

from .matcher import _find_command, _find_plugin, _match_command, _match_plugin
from .models import MAX_FULL_PROXY_CHARS, MAX_INDEX_CHARS, LLM_TEMPERATURE, IndexPlugin
from .parser import _extract_response_text
from .prompts import _FULL_PROXY_PROMPT, _REVIEW_PROMPT, _ROUTE_PROMPT, _TOPN_REFINE_PROMPT


# ── 数据结构 ──────────────────────────────────────

# LLM 路由结果: (plugin_name, command_name, args_str, confirmation_message)
RouteResult = Tuple[str, str, str, str]


# ── 命令索引文本构建 ────────────────────────────────


def build_index_text(plugins: List[IndexPlugin], max_chars: int = MAX_INDEX_CHARS) -> str:
    """
    将命令索引构建为 LLM 可读的文本格式。
    每条命令包含：命令名、参数、参数说明、命令描述、使用示例。
    格式设计使 LLM 能准确区分相似命令并理解参数含义。
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

            # 功能描述（独立一行，最醒目）
            if cmd_desc and cmd_desc != "无描述":
                line += f"\n       功能: {cmd_desc}"

            # 参数说明（独立一行）
            if args_desc:
                line += f"\n       参数说明: {args_desc}"

            # 过滤器类型（非标准 command 类型时标注）
            if ftype and ftype not in ("command",):
                line += f"\n       类型: {ftype}"

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
    param_names = re.findall(r'\[([^\]]+)\]', args)
    flags = re.findall(r'(-\w)', args)

    for pname in param_names:
        pname_lower = pname.lower()
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
            example_args.append(f"<{pname}>")

    if example_args:
        return f"{command} {' '.join(example_args)}"
    elif flags:
        return f"{command} {flags[0]}"
    else:
        return f"{command}"


# ── LLM 命令级路由 ──────────────────────────────────


async def llm_resolve(
    query: str,
    data: Dict[str, dict],
    provider,
    enable_review: bool = True,
    exclude: Optional[List[str]] = None,
) -> Optional[RouteResult]:
    """
    使用 LLM 将自然语言路由到具体命令（三步 TOP-N 流程）。

    第一步：从全部命令索引中粗筛 TOP-3 候选
    第二步：将候选详情 + 用户原始意图发给 LLM，让 LLM 精选并提取参数
    第三步：自校对 — 验证选中的命令是否匹配用户意图，不匹配则重新选择

    exclude: 要排除的命令列表，格式 ['插件名|命令名', ...]（用户之前拒绝过的）
    返回 (plugin_name, command_name, args_str, confirmation_message) 或 None。
    plugin_name == "__list_all__" 表示用户想看全部。
    """
    if not data or not provider:
        return None

    plugins = _dict_to_index(data)
    index_text = build_index_text(plugins)
    if not index_text:
        return None

    prompt = _ROUTE_PROMPT.format(index=index_text, query=query)

    if exclude:
        lines = []
        for e in exclude:
            parts = e.split("|", 1)
            lines.append(f"  - {parts[0]} / {parts[1]}" if len(parts) > 1 else f"  - {parts[0]}")
        prompt += "\n\n【以下命令已被用户拒绝，请不要选择：】\n" + "\n".join(lines)

    try:
        # ── 第一步：粗筛 TOP-N 候选 ──
        resp = await provider.text_chat(prompt, temperature=LLM_TEMPERATURE)
        raw = _extract_response_text(resp)
        if not raw:
            return None

        # 处理特殊指令
        if raw.upper().startswith("LIST_ALL"):
            return ("__list_all__", "-", "-", "将为你展示所有命令")
        if raw.upper().startswith("NONE"):
            return None

        # 解析所有候选
        candidates = _parse_topn_candidates(raw, plugins)
        if not candidates:
            fallback = _parse_single_route_line(raw.strip(), plugins)  # 兜底：按单行解析
            if fallback and exclude and _is_excluded(fallback, exclude):
                return None
            return fallback

        # 过滤已排除的候选
        if exclude:
            candidates = [c for c in candidates if not _is_excluded(c, exclude)]
            if not candidates:
                return None

        # ── 第二步：精选 + 参数提取 ──
        candidate_text = _format_candidates(candidates, plugins)
        refine_prompt = _TOPN_REFINE_PROMPT.format(
            candidates=candidate_text, query=query
        )

        resp2 = await provider.text_chat(refine_prompt, temperature=LLM_TEMPERATURE)
        raw2 = _extract_response_text(resp2)
        if not raw2:
            logger.debug("TOP-N 第二步未返回结果，使用第一步首个候选")
            return candidates[0]

        refined = _parse_single_route_line(raw2.strip(), plugins)
        if not refined or (exclude and _is_excluded(refined, exclude)):
            logger.debug("TOP-N 第二步解析失败或被排除，使用第一步首个候选")
            for c in candidates:
                if not exclude or not _is_excluded(c, exclude):
                    return c
            return None

        # ── 第三步：自校对 ──
        if enable_review:
            sel_text = _format_selection_for_review(refined, plugins)
            review_prompt = _REVIEW_PROMPT.format(
                query=query, selection=sel_text, candidates=candidate_text
            )

            resp3 = await provider.text_chat(review_prompt, temperature=LLM_TEMPERATURE)
            raw3 = _extract_response_text(resp3)
            if raw3:
                review_result = raw3.strip()
                if review_result.upper().startswith("CORRECT"):
                    logger.debug(f"LLM 自校对通过: {refined[0]}/{refined[1]}")
                    return _check_and_fix_mismatch(refined, query, candidates, plugins)
                # 校对不通过，解析新选择
                new_result = _parse_single_route_line(review_result, plugins)
                if new_result:
                    logger.info(f"LLM 自校对修正: {refined[0]}/{refined[1]} → {new_result[0]}/{new_result[1]}")
                    return _check_and_fix_mismatch(new_result, query, candidates, plugins)

            # 校对无明确结论，沿用第二步结果
            logger.debug(f"LLM 自校对无结论，沿用第二步结果: {refined[0]}/{refined[1]}")

        return _check_and_fix_mismatch(refined, query, candidates, plugins)

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


def _parse_topn_candidates(raw: str, plugins: List[IndexPlugin]) -> List[RouteResult]:
    """解析 TOP-N 候选行，返回所有有效候选列表"""
    raw = raw.strip()
    if raw.upper().startswith("NONE") or raw.upper().startswith("LIST_ALL"):
        return []
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    candidates: List[RouteResult] = []
    for line in lines:
        parsed = _parse_single_route_line(line, plugins)
        if parsed:
            candidates.append(parsed)
    return candidates


def _format_candidates(candidates: List[RouteResult], plugins: List[IndexPlugin]) -> str:
    """将候选列表格式化为带详细信息的文本，供 LLM 精选"""
    details: List[str] = []
    for i, (pk, cn, _, _) in enumerate(candidates, 1):
        pinfo = _find_plugin(pk, plugins)
        pdesc = pinfo.get("description", "") if pinfo else ""

        cmd_info = _find_command(cn, pinfo) if pinfo and cn else None

        # 构建展示
        display = pinfo.get("name", pk) if pinfo else pk
        cmd_str = cn if cn else "-"
        if cmd_info:
            args = cmd_info.get("args", "").strip()
            if args:
                cmd_str += f" {args}"
            detail_parts = _cmd_detail_lines(cmd_info)
            if detail_parts:
                cmd_str += f"\n     {'; '.join(detail_parts)}"

        line = f"{i}. {display} | {cmd_str}"
        if pdesc:
            line += f"\n   插件描述: {pdesc}"
        details.append(line)
    return "\n".join(details)


def _format_selection_for_review(
    selection: RouteResult, plugins: List[IndexPlugin]
) -> str:
    """将选中的命令格式化为详细信息，供自校对步骤使用"""
    pk, cn, args, msg = selection

    pinfo = _find_plugin(pk, plugins)
    display = pinfo.get("name", pk) if pinfo else pk
    lines = [f"插件: {display}"]

    cmd_info = _find_command(cn, pinfo) if pinfo and cn else None

    cmd_str = cn if cn else "-"
    if args:
        cmd_str += f" {args}"
    lines.append(f"命令: {cmd_str}")

    if cmd_info:
        lines.extend(_cmd_detail_lines(cmd_info))

    lines.append(f"确认语: {msg}")
    return "\n".join(lines)


def _check_and_fix_mismatch(
    result: RouteResult,
    query: str,
    candidates: List[RouteResult],
    plugins: List[IndexPlugin],
) -> RouteResult:
    """
    确定性兜底校验：自动从命令描述中提取否定短语（如"不含xx"、"不能查看xx"），
    与用户意图做匹配，发现冲突时从候选中找替代命令。
    """
    pk, cn, args, msg = result

    # 找到选中命令的功能描述
    pinfo = _find_plugin(pk, plugins)
    if not pinfo:
        return result
    cmd_info = _find_command(cn, pinfo)
    if not cmd_info:
        return result
    desc = cmd_info.get("description", "")

    if not desc:
        return result

    # 从描述中提取否定短语后的关键词
    # 如 "不含命令详情" → "命令详情"，"不能查看指令" → "查看指令"
    negation_markers = ("不含", "不包含", "不能查看", "不能显示", "不支持", "无法")
    conflict_phrases = set()
    for marker in negation_markers:
        idx = desc.find(marker)
        while idx != -1:
            phrase = desc[idx + len(marker):].strip()
            # 取到下一个标点或句尾
            for sep in ("，", "。", "；", "、", "\n"):
                if sep in phrase:
                    phrase = phrase[:phrase.index(sep)]
            if phrase:
                conflict_phrases.add(phrase)
            idx = desc.find(marker, idx + 1)

    if not conflict_phrases:
        return result

    # 检查用户意图是否触及了这些否定内容
    conflict = any(phrase in query for phrase in conflict_phrases)

    if not conflict:
        return result

    # 发现冲突，从候选中找替代
    for alt_pk, alt_cn, alt_args, alt_msg in candidates:
        if alt_cn == cn and alt_pk == pk:
            continue
        alt_pinfo = _find_plugin(alt_pk, plugins)
        if not alt_pinfo:
            continue
        alt_cmd = _find_command(alt_cn, alt_pinfo)
        alt_desc = alt_cmd.get("description", "") if alt_cmd else ""
        # 替代命令的描述不包含同样的否定短语 → 可用
        if not any(phrase in alt_desc for phrase in conflict_phrases):
            logger.info(
                f"确定性兜底修正: {pk}/{cn} → {alt_pk}/{alt_cn} "
                f"(冲突短语: {conflict_phrases})"
            )
            return (alt_pk, alt_cn, alt_args, alt_msg)

    return result



def _is_excluded(result: RouteResult, exclude: List[str]) -> bool:
    """检查路由结果是否在排除列表中"""
    pk, cn, _, _ = result
    return f"{pk}|{cn}" in exclude


def _resolve_plugin_display(plugin_part: str, plugins: List[IndexPlugin]) -> Tuple[str, str]:
    """将 LLM 返回的插件名解析为 (found_key, display_name)。"""
    cache_key = _match_plugin(plugin_part, plugins)
    pinfo = _find_plugin(cache_key or plugin_part, plugins)
    if pinfo:
        return pinfo.get("key", "") or pinfo.get("name", ""), pinfo.get("name", plugin_part)
    return cache_key or plugin_part, plugin_part


def _cmd_detail_lines(cmd_info: dict) -> List[str]:
    """从命令信息中提取功能描述和参数说明，返回格式化行列表。"""
    parts = []
    desc = cmd_info.get("description", "").strip()
    if desc and desc != "无描述":
        parts.append(f"功能: {desc}")
    adesc = cmd_info.get("args_description", "").strip()
    if adesc:
        parts.append(f"参数说明: {adesc}")
    return parts


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
            pinfo = _find_plugin(cache_key, plugins)
            display = pinfo.get("name", cache_key) if pinfo else cache_key
            return (cache_key, "-", "", f"找到插件 **{display}**，将展示其所有命令")
        return None

    plugin_part, cmd_part, args_part, msg_part = parts[0], parts[1], parts[2], parts[3]

    cache_key = _match_plugin(plugin_part, plugins)
    found_key, display = _resolve_plugin_display(plugin_part, plugins)

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


# ── LLM 全权代理 ──────────────────────────────────


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
    cache_text = build_index_text(plugins, max_chars=MAX_FULL_PROXY_CHARS)
    if not cache_text:
        return None

    prompt = _FULL_PROXY_PROMPT.format(cache=cache_text, query=query)

    try:
        resp = await provider.text_chat(prompt, temperature=LLM_TEMPERATURE)
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
    found_key, display = _resolve_plugin_display(plugin_part, plugins)

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
