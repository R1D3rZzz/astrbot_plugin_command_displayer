import re
from typing import Dict, List, Optional
from astrbot.api import logger
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.regex import RegexFilter

from .models import PluginInfo, CommandEntry, SOURCE_DIRECT


def read_registered_commands(context) -> Dict[str, PluginInfo]:
    """直接从 star_handlers_registry 读取所有已注册插件的指令和行为"""
    handlers = star_handlers_registry._handlers
    if not handlers:
        logger.debug("star_handlers_registry 中无已注册 handler")
        return {}

    star_names = _build_star_name_map(context)
    # 美化内置插件名
    for handler_md in handlers:
        plugin_id, _ = _resolve_plugin_id(handler_md.handler_full_name)
        if plugin_id not in star_names:
            pretty = _prettify_builtin_name(plugin_id)
            if pretty:
                star_names[plugin_id] = pretty
    plugins: Dict[str, PluginInfo] = {}

    for handler_md in handlers:
        plugin_id, _ = _resolve_plugin_id(handler_md.handler_full_name)
        display_name = star_names.get(plugin_id, plugin_id)

        if display_name not in plugins:
            plugins[display_name] = {
                "name": display_name,
                "description": "",
                "commands": [],
                "source": SOURCE_DIRECT,
            }

        cmd_entry = _extract_command(handler_md)
        if cmd_entry:
            plugins[display_name]["commands"].append(cmd_entry)

    return {k: v for k, v in plugins.items() if v["commands"]}


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
    """从 handler_full_name 提取插件 ID 和模块路径"""
    parts = handler_full_name.rsplit("_", 1)
    module_path = parts[0] if len(parts) > 1 else handler_full_name

    path_parts = module_path.split(".")
    if len(path_parts) >= 3 and path_parts[0] == "data" and path_parts[1] == "plugins":
        return path_parts[2], module_path

    return module_path, module_path


def _prettify_builtin_name(module_path: str) -> Optional[str]:
    """将内置模块路径转为简短显示名"""
    # astrbot.builtin_stars.builtin_commands.main → main (内置)
    if module_path.startswith("astrbot.builtin_stars.builtin_commands."):
        short = module_path.split(".")[-1]
        return f"{short} (内置)"
    # astrbot.builtin_stars.xxx → xxx (内置)
    if module_path.startswith("astrbot.builtin_stars."):
        short = module_path.split(".")[-1]
        return f"{short} (内置)"
    return None


def _extract_command(handler_md) -> Optional[CommandEntry]:
    """从 handler 的 event_filters 中提取指令信息"""
    command_name = ""
    aliases: List[str] = []
    is_regex = False
    filter_type = "unknown"

    for f in handler_md.event_filters:
        if isinstance(f, CommandFilter):
            filter_type = "command"
            raw_name = getattr(f, "command_name", "")
            # 确保有 / 前缀
            command_name = raw_name if raw_name.startswith("/") else f"/{raw_name}"
            if hasattr(f, "aliases"):
                for a in f.aliases:
                    aliases.append(a.pattern if isinstance(a, re.Pattern) else str(a))
        elif isinstance(f, RegexFilter):
            filter_type = "regex"
            is_regex = True
            raw_regex = getattr(f, "regex", "")
            # 提取纯模式字符串（可能是 re.Pattern 对象）
            if isinstance(raw_regex, re.Pattern):
                command_name = raw_regex.pattern
            else:
                command_name = str(raw_regex)
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
        args="",
        aliases=aliases,
        description=desc_first_line,
        is_regex=is_regex,
        source=SOURCE_DIRECT,
    )
