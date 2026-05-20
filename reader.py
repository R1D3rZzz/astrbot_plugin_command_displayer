"""从 star_handlers_registry 直接读取已注册插件的指令和行为"""

import inspect
import re
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.regex import RegexFilter

from .models import CommandEntry, PluginInfo, SOURCE_DIRECT


def read_registered_commands(context) -> Dict[str, PluginInfo]:
    """直接从 star_handlers_registry 读取所有已注册插件的指令"""
    handlers = star_handlers_registry._handlers
    if not handlers:
        logger.debug("star_handlers_registry 中无已注册 handler")
        return {}

    star_names = _build_star_name_map(context)
    star_names.setdefault("main_builtin", "main (内置)")

    for handler_md in handlers:
        plugin_id, module_path = _resolve_plugin_id(handler_md.handler_full_name)
        if plugin_id not in star_names:
            pretty = _prettify_builtin_name(module_path)
            if pretty:
                star_names[plugin_id] = pretty

    plugins: Dict[str, PluginInfo] = {}

    for handler_md in handlers:
        plugin_id, _ = _resolve_plugin_id(handler_md.handler_full_name)
        display_name = star_names.get(plugin_id, plugin_id)
        key = plugin_id

        if key not in plugins:
            plugins[key] = {
                "name": display_name,
                "description": "",
                "commands": [],
                "source": SOURCE_DIRECT,
            }

        cmd_entry = _extract_command(handler_md)
        if cmd_entry:
            plugins[key]["commands"].append(cmd_entry)

    return {k: v for k, v in plugins.items() if v["commands"]}


# ── 插件名映射 ──────────────────────────────────────


def _build_star_name_map(context) -> Dict[str, str]:
    """构建 插件目录名 → 显示名 的映射"""
    mapping = {}
    try:
        for star_md in context.get_all_stars():
            root_dir = getattr(star_md, "root_dir_name", "")
            display = getattr(star_md, "display_name", "")
            if root_dir:
                mapping[root_dir] = display or root_dir
    except Exception:
        pass
    return mapping


def _resolve_plugin_id(handler_full_name: str) -> tuple:
    """从 handler_full_name 提取 (插件ID, 模块路径)"""
    if handler_full_name.startswith("astrbot.builtin_stars."):
        return "main_builtin", handler_full_name

    parts = handler_full_name.rsplit("_", 1)
    module_path = parts[0] if len(parts) > 1 else handler_full_name
    path_parts = module_path.split(".")

    if len(path_parts) >= 3 and path_parts[0] == "data" and path_parts[1] == "plugins":
        return path_parts[2], module_path

    return module_path, module_path


def _prettify_builtin_name(module_path: str) -> Optional[str]:
    """将内置模块路径转为简短显示名"""
    if module_path.startswith("astrbot.builtin_stars."):
        return "main (内置)"
    return None


# ── 命令提取 ──────────────────────────────────────


def _extract_command(handler_md) -> Optional[CommandEntry]:
    """从 handler 的 event_filters 中提取指令信息，包括完整参数"""
    command_name = ""
    aliases: List[str] = []
    is_regex = False
    filter_type = "unknown"
    args_str = ""
    args_description = ""

    for f in handler_md.event_filters:
        if isinstance(f, CommandFilter):
            filter_type = "command"
            raw_name = getattr(f, "command_name", "")
            command_name = raw_name if raw_name.startswith("/") else f"/{raw_name}"
            if hasattr(f, "aliases"):
                for a in f.aliases:
                    aliases.append(a.pattern if isinstance(a, re.Pattern) else str(a))

            # 从 handler_params 提取参数名和默认值
            if hasattr(f, "handler_params") and f.handler_params:
                args_str, args_description = _format_handler_params(f.handler_params)

        elif isinstance(f, RegexFilter):
            filter_type = "regex"
            is_regex = True
            raw_regex = getattr(f, "regex", "")
            command_name = raw_regex.pattern if isinstance(raw_regex, re.Pattern) else str(raw_regex)
        else:
            filter_type = type(f).__name__
            for attr in ("desc", "description", "message", "event_type", "platform"):
                if hasattr(f, attr):
                    command_name = f"{attr}: {getattr(f, attr)}"
                    break

    if not handler_md.event_filters:
        filter_type = "on_all_message"
        command_name = "[监听所有消息]"

    desc_first_line = (handler_md.desc or "无描述").split("\n")[0].strip()

    return CommandEntry(
        filter_type=filter_type,
        command=command_name,
        args=args_str,
        args_description=args_description,
        aliases=aliases,
        description=desc_first_line,
        is_regex=is_regex,
        source=SOURCE_DIRECT,
    )


def _format_handler_params(params: dict) -> tuple:
    """
    将 handler_params 格式化为 (args_str, args_description)。

    handler_params 格式: {参数名: 默认值} 或 {参数名: 类型}（无默认值时表示必填）

    返回:
        args_str: 如 "[subcmd] [arg]" 或 "[-s] [-d] [-t]"
        args_description: 如 "subcmd: 子命令; arg: 附加参数"
    """
    if not params:
        return "", "无参数"

    arg_parts = []
    desc_parts = []

    for name, default in params.items():
        # 判断是必填（值为类型）还是可选（值为默认值）
        if isinstance(default, type):
            # 必填参数，无默认值
            arg_parts.append(f"[{name}]")
            desc_parts.append(f"{name}: 必填参数")
        elif default is None:
            arg_parts.append(f"[{name}]")
            desc_parts.append(f"{name}: 可选，默认空")
        elif isinstance(default, bool):
            # 布尔 flag，如 -s / -d / -t
            flag = _name_to_flag(name)
            arg_parts.append(f"[{flag}]")
            desc_parts.append(f"{flag}: 布尔开关，默认 {'开启' if default else '关闭'}")
        elif isinstance(default, (int, float)):
            arg_parts.append(f"[{name}]")
            desc_parts.append(f"{name}: 数值，默认 {default}")
        else:
            # 字符串等有默认值的参数
            arg_parts.append(f"[{name}]")
            desc_parts.append(f"{name}: 可选，默认 \"{default}\"")

    return " ".join(arg_parts), "; ".join(desc_parts)


def _name_to_flag(name: str) -> str:
    """将参数名转换为 flag 格式，如 verbose → -v，detail → -d"""
    # 常见映射
    shortcuts = {
        "simple": "-s",
        "detailed": "-d",
        "table": "-t",
        "verbose": "-v",
        "force": "-f",
        "quiet": "-q",
        "recursive": "-r",
        "all": "-a",
        "output": "-o",
        "input": "-i",
        "config": "-c",
        "help": "-h",
        "version": "-V",
    }
    lower = name.lower()
    if lower in shortcuts:
        return shortcuts[lower]
    # 默认取首字母
    return f"-{name[0]}"
