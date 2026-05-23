"""命令执行器：通过事件队列注入模拟用户发送命令"""

import copy

from astrbot.api import logger
from astrbot.core.message.components import Plain


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
